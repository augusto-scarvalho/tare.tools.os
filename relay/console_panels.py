"""Shared presentation for conversation-bound supporting screens."""
from textual.screen import ModalScreen


class SupportPanel(ModalScreen):
    DEFAULT_CSS = '''
    SupportPanel { align: center middle; background: $background 80%; }
    SupportPanel .support-body { width: 96%; max-width: 120; height: 94%; padding: 0 1;
                                background: $background; border: round $primary 35%; }
    SupportPanel.narrow .support-body { width: 100%; height: 100%; border: none; }
    SupportPanel .panel-header { height: 1; margin: 0 0 1 0; }
    SupportPanel .panel-title { width: 1fr; height: 1; text-style: bold; color: $foreground; margin: 0; }
    SupportPanel .support-body Button { height: 1; min-height: 1; width: auto; min-width: 16; padding: 0 1;
                          border: none; background: transparent; color: $text-muted; text-style: none; }
    SupportPanel .support-body Button:hover, SupportPanel .support-body Button:focus,
    SupportPanel .support-body Button.-active { border: none; background: $primary 20%; color: $foreground; }
    SupportPanel .support-body Button.panel-primary { color: $primary; text-style: bold; }
    SupportPanel .support-body Button.panel-danger { color: $warning; }
    SupportPanel .support-body Button:disabled { color: $text-muted 50%; background: transparent; }
    SupportPanel .panel-actions { height: 1; margin: 1 0 0 0; align-horizontal: right; }
    SupportPanel .panel-actions Button { margin-left: 1; }
    SupportPanel Select { height: 1; margin: 0; }
    SupportPanel SelectCurrent { height: 1; border: none; padding: 0 1; background: $panel; }
    SupportPanel Select:focus > SelectCurrent,
    SupportPanel Select.-expanded > SelectCurrent { border: none !important; background: $primary 20%; }
    SupportPanel .support-body Input { height: 1; min-height: 1; border: none; padding: 0 1; background: $panel; }
    SupportPanel .support-body Input:focus { border: none; background: $primary 15%; }
    '''

    def on_resize(self, event):
        self.set_class(event.size.width < 100 or event.size.height < 30, 'narrow')
