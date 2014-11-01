from otree import constants
from otree.common import get_session_module
from django.conf import settings
from django.utils.importlib import import_module
from otree.models.user import Experimenter
from otree.session.models import Session, SessionExperimenter, Participant
from django.db import transaction
from collections import defaultdict
from itertools import groupby
import re

def gcd(a, b):
    """Return greatest common divisor using Euclid's Algorithm."""
    while b:
        a, b = b, a % b
    return a

def lcm(a, b):
    """Return lowest common multiple."""
    return a * b // gcd(a, b)

def lcmm(*args):
    """Return lcm of args."""
    return reduce(lcm, args)

class SessionType(object):
    def __init__(self, name, subsession_apps, base_pay, participants_per_session,
                 display_name=None,
                 participants_per_demo_session = None, is_for_mturk=False, doc=None, assign_to_groups_on_the_fly=False):

        if not re.match(r'^\w+$', name):
            raise ValueError('Session "{}": name must be alphanumeric with no spaces.'.format(name))

        self.name = name



        self.display_name = display_name or name

        if len(subsession_apps) == 0:
            raise ValueError('Need at least one subsession.')

        self.subsession_apps = subsession_apps
        self.base_pay = base_pay
        self.participants_per_demo_session = participants_per_demo_session
        self.participants_per_session = participants_per_session
        self.is_for_mturk = is_for_mturk
        self.doc = doc.strip()

        # on MTurk, assign_to_groups_on_the_fly = True
        self.assign_to_groups_on_the_fly = assign_to_groups_on_the_fly

    def lcm(self):
        participants_per_group_list = []
        for app_label in self.subsession_apps:
            models_module = import_module('{}.models'.format(app_label))
            # if players_per_group is None, 0, etc.
            players_per_group = models_module.Constants.players_per_group or 1
            participants_per_group_list.append(players_per_group)
        return lcmm(*participants_per_group_list)


class SessionTypeDirectory(object):
    def __init__(self, demo_only=False):
        self.demo_only = demo_only
        self.session_types_as_dict = {session_type.name.lower(): session_type for session_type in self.select(demo_only)}

    def select(self, demo_only = False):
        session_types = get_session_module().session_types()
        if demo_only:
            return [session_type for session_type in session_types if get_session_module().show_on_demo_page(session_type.name)]
        else:
            return session_types

    def get_item(self, session_type_name):
        return self.session_types_as_dict[session_type_name.lower()]

@transaction.atomic
def create_session(type_name, label='', num_participants=None, special_category=None, preassign_players_to_groups=False):
    """2014-5-2: i could implement this by overriding the __init__ on the Session model, but I don't really know how that works,
    and it seems to be a bit discouraged:
    https://docs.djangoproject.com/en/1.4/ref/models/instances/#django.db.models.Model
    2014-9-22: preassign to groups for demo mode.
    """
    try:
        session_type = SessionTypeDirectory().get_item(type_name)
    except KeyError:
        raise ValueError('Session type "{}" not found in sessions.py'.format(type_name))
    session = Session(
        type_name=session_type.name,
        label=label,
        is_for_mturk=session_type.is_for_mturk,
        base_pay=session_type.base_pay,
        special_category=special_category,
    )

    session.save()

    session_experimenter = SessionExperimenter()
    session_experimenter.save()
    session.session_experimenter = session_experimenter

    participants = []

    if special_category == constants.special_category_demo:
        participants_per_session = session_type.participants_per_demo_session
    else:
        #FIXME: 0 vs None
        participants_per_session = num_participants or session_type.participants_per_session

    # check that it divides evenly
    if participants_per_session % session_type.lcm():
        raise ValueError('Number of participants does not divide evenly')


    for i in range(participants_per_session):
        participant = Participant(session = session)
        participant.save()
        participants.append(participant)

    subsessions = []
    for app_label in session_type.subsession_apps:
        if app_label not in settings.INSTALLED_OTREE_APPS:
            raise ValueError('Your session contains a subsession app named "{}". You need to add this to INSTALLED_OTREE_APPS in settings.py.'.format(app_label))

        models_module = import_module('{}.models'.format(app_label))

        for round_number in range(1, models_module.Constants.number_of_rounds+1):
            subsession = models_module.Subsession(
                round_number = round_number,
                )
            subsession.save()

            session.add_subsession(subsession)

            experimenter = Experimenter(session=session)
            experimenter.subsession = subsession
            experimenter.save()
            subsession._experimenter = experimenter

            subsession.save()

            for i in range(participants_per_session):
                player = models_module.Player(
                    subsession = subsession,
                    session = session,
                    participant = participants[i]
                )
                player.save()

            if session.type().assign_to_groups_on_the_fly:
                # create groups at the beginning because we will not need to delete players
                # unlike the lab setting, where there may be no-shows
                subsession._create_empty_groups()

            subsessions.append(subsession)

    session.chain_subsessions(subsessions)
    session.chain_players()
    session.session_experimenter.chain_experimenters()
    if preassign_players_to_groups:
        session._assign_groups_and_initialize()
    session.ready = True
    session.save()
    return session
