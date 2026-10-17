"""FSM state groups."""

from aiogram.fsm.state import State, StatesGroup


class NewEventSG(StatesGroup):
    date = State()
    time = State()
    title = State()
    kind = State()


class EditEventSG(StatesGroup):
    title = State()
    date = State()
    time = State()


class TemplateSG(StatesGroup):
    title = State()
    kind = State()
    time = State()


class StudentSG(StatesGroup):
    add_name = State()
    add_year = State()
    add_joined = State()
    import_text = State()
    import_date = State()
    edit_name = State()
    edit_year = State()
    edit_joined = State()


class ExcuseSG(StatesGroup):
    kind = State()
    date_from = State()
    date_to = State()
    comment = State()


class RequestSG(StatesGroup):
    text = State()


class RejectSG(StatesGroup):
    comment = State()


class MonthSG(StatesGroup):
    rename = State()


class PickSG(StatesGroup):
    filter = State()


class WipeSG(StatesGroup):
    warned = State()
    word = State()
