"""/quota: what is left of each subscription and API credit (tare quota), and where the owner states a balance or
the energy price. The text is provider_quota.render's, the same as the CLI and /usage; no key is shown or asked for."""
import os

from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from .console_panels import SupportPanel


def row(label, field):
    return Horizontal(Static(label, markup=False), field, classes='quota-row')


def read(text, what, cast):
    try:
        return cast(text)
    except ValueError:
        raise ValueError(f'{what} {text!r} is not valid') from None


class QuotaPanel(SupportPanel):
    BINDINGS = [Binding('escape', 'close', 'Back', priority=True)]
    DEFAULT_CSS = '''
    QuotaPanel #quota-scroll { height: 1fr; }
    QuotaPanel #quota-accounts { height: auto; }
    QuotaPanel .quota-name { height: auto; margin-top: 1; text-style: bold; }
    QuotaPanel .quota-note { height: auto; margin-top: 1; }
    QuotaPanel .quota-row { height: 1; }
    QuotaPanel .quota-row Static { width: 30; }
    QuotaPanel .quota-row Input { width: 1fr; }
    QuotaPanel #quota-status { height: auto; max-height: 4; }
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = []  # (account, name) of the API accounts that take a stated balance
        self.price = ''  # the energy price as saved, to save it only when it changed

    def compose(self):
        from . import provider_spend
        price = provider_spend.local_energy().get('price_kwh')
        self.price = f'{price:g}' if price else ''
        with Vertical(classes='support-body'):
            with Horizontal(classes='panel-header'):
                yield Static('Quota, credit and cost', classes='panel-title')
                yield Button('Back · Esc', id='quota-close')
            with VerticalScroll(id='quota-scroll'):
                yield Static(id='quota-text', markup=False)
                yield Static('State a balance only for an account tare should count down from: stating it restarts '
                             'the count. A day renews the credit each month; a date lapses it, once.',
                             classes='quota-note')
                yield Static("For Anthropic's or OpenAI's own count of the organization, store an admin key of it in a "
                             'terminal: tare setup --credential ANTHROPIC_ADMIN_KEY (or OPENAI_ADMIN_KEY). It is sent '
                             'to the cost report only, never to a model.', classes='quota-note', markup=False)
                yield Vertical(id='quota-accounts')
                yield Static('Local models', classes='quota-name')
                yield row('Energy price, US$ per kWh', Input(self.price, id='quota-energy'))
            yield Static('', id='quota-status', markup=False)
            with Horizontal(classes='panel-actions'):
                yield Button('Read again', id='quota-read')
                yield Button('Save', id='quota-save', classes='panel-primary')

    async def on_mount(self):
        from . import provider_quota
        await self.show(provider_quota.cached())
        # Only the interactive entry asks the vendors' CLIs, like /usage; Read again always does.
        if self.app.asks_providers and os.environ.get('TARE_CLI_DISCOVERY') != '0':
            self.read_again()

    async def show(self, report):
        from . import provider_quota, provider_spend
        self.query_one('#quota-text', Static).update(provider_quota.render(report) if report else 'No reading yet.')
        accounts = [(a['account'], a['name']) for a in (report or {}).get('accounts', [])
                    if a['kind'] == 'credit' and a['account'] in provider_spend.DECLARABLE]
        if accounts == self.accounts:  # the fields keep what the owner typed while a reading lands
            return
        self.accounts = accounts
        box = self.query_one('#quota-accounts', Vertical)
        await box.remove_children()
        await box.mount(*[widget for account, name in accounts for widget in (
            Static(name, classes='quota-name', markup=False),
            row('Balance, US$', Input(id=f'quota-balance-{account}')),
            row('Renews on day', Input(id=f'quota-day-{account}')),
            row('or lapses on (YYYY-MM-DD)', Input(id=f'quota-date-{account}')))])

    @work(thread=True, group='quota-read')
    def read_again(self, saved=''):
        from . import provider_quota
        self.app.call_from_thread(self.say, saved + 'Reading each provider again; no model call is made.')
        try:
            report = provider_quota.refresh()
        except Exception as exc:  # the saved reading stays on screen
            self.app.call_from_thread(self.say, f'The providers could not be read: {type(exc).__name__}.')
            return
        if self.is_attached:
            self.app.call_from_thread(self.show, report)
            self.app.call_from_thread(self.say, saved + 'Read again.')

    def say(self, text):
        if self.is_attached:
            self.query_one('#quota-status', Static).update(text)

    def stated(self):
        """What Save would write, checked in full first: ([(account, amount, kwargs)], the new price or None).
        Raises ValueError naming the account when anything is wrong, so that nothing is saved."""
        from . import provider_quota, provider_spend
        plan = []
        for account, name in self.accounts:
            balance, day, date = (self.query_one(f'#quota-{field}-{account}', Input).value.strip()
                                  for field in ('balance', 'day', 'date'))
            try:
                if not balance:  # nothing is restated unasked: stating a balance restarts the count
                    if day or date:
                        raise ValueError('a day or a date needs a balance')
                    continue
                if day and date:
                    raise ValueError('state a renewal day or a lapse date, not both')
                amount = read(balance, 'the balance', float)
                kwargs = ({'renews': (read(day, 'the day', int), amount)} if day else
                          {'expires': read(date, 'the date', provider_quota.day_end)} if date else {})
                provider_spend.declare(account, amount, dry=True, **kwargs)
            except ValueError as exc:
                raise ValueError(f'{name}: {exc}') from None
            plan.append((account, amount, kwargs))
        price = self.query_one('#quota-energy', Input).value.strip()
        if price and price != self.price:
            from .environment import set_local_energy
            try:
                set_local_energy({}, 'price_kwh', price)
            except ValueError as exc:
                raise ValueError(f'Energy price: {exc}') from None
        return plan, price if price and price != self.price else None

    def save(self):
        from . import provider_spend
        from .environment import set_local_energy, update
        try:
            plan, price = self.stated()
            if not plan and not price:
                self.say('Nothing to save: fill a Balance, or change the energy price.')
                return
            if price:  # first: it can fail on a lock, and the balances were already checked
                update(lambda value: set_local_energy(value, 'price_kwh', price))
                self.price = price
            for account, amount, kwargs in plan:
                provider_spend.declare(account, amount, **kwargs)
        except (ValueError, OSError) as exc:
            self.say(f'{exc} Nothing was saved.')
            return
        for field in self.query(Input):  # a stated balance is not stated again by the next Save
            if field.id != 'quota-energy':
                field.value = ''
        self.read_again('Saved: ' + ', '.join([dict(self.accounts)[a] for a, *_ in plan] + ['energy price'] * bool(price))
                        + '. ')

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'quota-close':
            self.action_close()
        elif event.button.id == 'quota-read':
            self.read_again()
        else:
            self.save()

    def action_close(self):
        self.dismiss(None)
