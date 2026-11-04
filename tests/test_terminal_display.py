from relay.terminal_display import EscapeFilter, TerminalDisplay


def test_terminal_rewrites_lines_styles_unicode_and_returns_cursor_queries():
    display = TerminalDisplay(30,4)
    replies = display.feed('10%\r\x1b[31m100%\x1b[0m\r\n界 café\x1b[6n')
    assert display.active.display[0].startswith('100%')
    assert display.active.display[1].startswith('界 café')
    assert replies == ['\x1b[2;8R']
    rendered = display.render()
    assert '界 café' in rendered.plain and '\x1b' not in rendered.plain
    assert any(span.style.color.name == 'red' for span in rendered.spans if span.style.color)


def test_alternate_screen_preserves_primary_and_keyboard_modes():
    display = TerminalDisplay(20,4)
    display.feed('shell prompt\x1b[?1049h\x1b[?1h\x1b[?2004hEDITOR')
    assert display.active is display.alternate and 'EDITOR' in display.render().plain
    assert display.key('up') == '\x1bOA'
    assert display.paste('hello') == '\x1b[200~hello\x1b[201~'
    display.feed('\x1b[?1049l\x1b[?1l\x1b[?2004l')
    assert display.active is display.primary and 'shell prompt' in display.render().plain
    assert 'EDITOR' not in display.render().plain
    assert display.key('up') == '\x1b[A' and display.key('ctrl+c') == '\x03'


def test_unterminated_controls_and_combining_input_stay_bounded():
    filtered = EscapeFilter()
    assert filtered.feed('before\x1b]52;c;') == 'before'
    for _ in range(20): assert filtered.feed('x'*20000) == ''
    assert len(filtered.pending) <= 129
    assert filtered.feed('\x1b\\after') == 'after'
    display = TerminalDisplay(20,4)
    display.feed('a'+'\u0301'*20000)
    assert len(display.render().plain) < 100
    display.feed('ignored',truncated=True)
    assert display.lost_output and 'ignored' in display.render().plain


def test_query_split_across_output_pages_and_bounded_scrollback():
    display = TerminalDisplay(20,4)
    assert display.feed('\x1b[') == []
    assert display.feed('6n') == ['\x1b[1;1R']
    display.feed('x\r\n'*500)
    assert len(display.primary.history.top) <= 300
    display.resize(25,6)
    assert display.primary.columns == 25 and display.alternate.lines == 6


def test_all_ansi_backgrounds_can_be_rendered():
    display = TerminalDisplay(40,4)
    for code in (*range(40,48),*range(100,108)):
        display.feed(f'\x1b[{code}mX')
    assert display.render().plain.count('X') == 16
