"""Tests for the string lexical errors reported by the pure-python backend.

The python backend lexes JSON strings itself (yajl does it in C), so these
tests exercise it directly. The three shapes covered are unescaped control
characters, invalid escape sequences, and byte sequences that are not valid
UTF-8. They must all be `JSONError` subclasses so that pipelines quarantining
bad records keep working, while truncation must keep raising exactly
`IncompleteJSONError` as before.
"""

import io

import pytest

import ijson
from ijson import common, utils

from .support.async_ import get_all as get_all_async

BACKEND = ijson.get_backend('python')


def _run_gen(content):
    return list(BACKEND.basic_parse_gen(io.BytesIO(content)))


def _run_coro(content):
    events = utils.sendable_list()
    coro = BACKEND.basic_parse_coro(events)
    for byte in content:
        coro.send(bytes([byte]))
    coro.close()
    return events


def _run_async(content):
    return get_all_async(BACKEND.basic_parse_async, content)


ENTRY_POINTS = [
    pytest.param(_run_gen, id='gen'),
    pytest.param(_run_coro, id='coro'),
    pytest.param(_run_async, id='async'),
]

CONTROL_CHARACTER_JSONS = [
    b'"line\nbreak"',
    b'"tab\there"',
    b'"null\x00byte"',
    b'"\x1funit separator"',
    b'[1, "a\tb"]',
]

INVALID_ESCAPE_JSONS = [
    br'"\q"',
    br'"\U1234"',
    br'"\x41"',
    br'"\u12G4"',
    br'"\u12"',
    br'{"k": "x\q"}',
]

INVALID_UTF8_JSONS = [
    b'"\xff"',
    b'"\xa8"',
    b'"caf\xe9"',
    b'"\xc3"',
    b'["\xff"]',
]


class TestErrorHierarchy:
    def test_lexical_errors_are_json_errors(self):
        for cls in (common.JSONLexicalError,
                    common.InvalidControlCharacterError,
                    common.InvalidEscapeSequenceError,
                    common.InvalidUTF8Error):
            assert issubclass(cls, common.JSONError)

    def test_concrete_errors_share_the_lexical_base(self):
        for cls in (common.InvalidControlCharacterError,
                    common.InvalidEscapeSequenceError,
                    common.InvalidUTF8Error):
            assert issubclass(cls, common.JSONLexicalError)

    def test_lexical_and_incomplete_errors_are_disjoint(self):
        """Invalid content must never be reported as truncation, and vice versa."""
        assert not issubclass(common.JSONLexicalError, common.IncompleteJSONError)
        assert not issubclass(common.IncompleteJSONError, common.JSONLexicalError)

    def test_public_exports(self):
        for name in ('JSONLexicalError',
                     'InvalidControlCharacterError',
                     'InvalidEscapeSequenceError',
                     'InvalidUTF8Error'):
            assert getattr(ijson, name) is getattr(common, name)

    def test_position_defaults_to_none(self):
        assert common.JSONLexicalError('message').position is None
        assert common.InvalidControlCharacterError('message').position is None


@pytest.mark.parametrize('run', ENTRY_POINTS)
@pytest.mark.parametrize('content', CONTROL_CHARACTER_JSONS)
def test_control_character(run, content):
    with pytest.raises(common.InvalidControlCharacterError) as excinfo:
        run(content)
    assert isinstance(excinfo.value, common.JSONError)
    assert not isinstance(excinfo.value, common.IncompleteJSONError)
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.parametrize('run', ENTRY_POINTS)
@pytest.mark.parametrize('content', INVALID_ESCAPE_JSONS)
def test_invalid_escape_sequence(run, content):
    with pytest.raises(common.InvalidEscapeSequenceError) as excinfo:
        run(content)
    assert isinstance(excinfo.value, common.JSONError)
    assert not isinstance(excinfo.value, common.IncompleteJSONError)
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.parametrize('run', ENTRY_POINTS)
@pytest.mark.parametrize('content', INVALID_UTF8_JSONS)
def test_invalid_utf8(run, content):
    with pytest.raises(common.InvalidUTF8Error) as excinfo:
        run(content)
    assert isinstance(excinfo.value, common.JSONError)
    assert not isinstance(excinfo.value, common.IncompleteJSONError)
    assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize('run', ENTRY_POINTS)
@pytest.mark.parametrize('content', [
    b'"a\nb"', b'[1, "a\tb"]', br'{"k": "x\q"}', br'"\u12G4"',
])
def test_lexical_errors_are_distinct_from_truncation(run, content):
    """The error class exposes the distinction a quarantine pipeline needs."""
    with pytest.raises(common.JSONError) as excinfo:
        run(content)
    assert isinstance(excinfo.value, common.JSONLexicalError)
    assert not isinstance(excinfo.value, common.IncompleteJSONError)


@pytest.mark.parametrize('run', ENTRY_POINTS)
@pytest.mark.parametrize('content', [
    b'"unterminated',
    b'"abc\\',
    b'"\xc3',            # UTF-8 sequence cut short
    b'"\xf0\x9f',        # ditto
    b'[1',
    b'{"a": fals',
    b'',
])
def test_truncation_keeps_incomplete_error(run, content):
    with pytest.raises(common.IncompleteJSONError) as excinfo:
        run(content)
    assert not isinstance(excinfo.value, common.JSONLexicalError)


@pytest.mark.parametrize('run', ENTRY_POINTS)
@pytest.mark.parametrize('content', [b'+1', b'+0', b'01', b'1.', b'.1', b'1e'])
def test_other_lexical_errors_keep_their_types(run, content):
    """Invalid numbers and symbols keep raising plain JSONError, not the new family."""
    with pytest.raises(common.JSONError) as excinfo:
        run(content)
    assert not isinstance(excinfo.value, common.JSONLexicalError)
    assert not isinstance(excinfo.value, common.IncompleteJSONError)


@pytest.mark.parametrize('run', ENTRY_POINTS)
def test_entry_points_agree(run):
    """All three entry points classify the same content identically."""
    content = b'[1, "a\tb"]'
    errors = []
    for entry_point in (lambda c: _run_gen(c),
                        lambda c: _run_coro(c),
                        lambda c: _run_async(c)):
        with pytest.raises(common.JSONError) as excinfo:
            entry_point(content)
        errors.append((type(excinfo.value), excinfo.value.position))
    assert len({error[0] for error in errors}) == 1
    assert len({error[1] for error in errors}) == 1


def test_control_character_position_is_document_offset():
    with pytest.raises(common.InvalidControlCharacterError) as excinfo:
        _run_gen(b'[1, "a\tb"]')
    # The tab is at index 6 of the whole document, not of the string lexeme.
    assert excinfo.value.position == 6


def test_invalid_escape_position_is_document_offset():
    with pytest.raises(common.InvalidEscapeSequenceError) as excinfo:
        _run_gen(br'{"k": "x\q"}')
    assert excinfo.value.position == 8


def test_invalid_utf8_has_no_position():
    with pytest.raises(common.InvalidUTF8Error) as excinfo:
        _run_gen(b'"\xff"')
    assert excinfo.value.position is None


class ChunkedFile:
    """A bytes file returning one chunk per read(), honouring read(0)."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, n=-1):
        if n == 0:
            return b''
        return self.chunks.pop(0) if self.chunks else b''


@pytest.mark.parametrize('content, expected', [
    # A multi-byte sequence split across reads is fine...
    ([b'"\xe4', b'\xb8', b'\x96"'], None),
    # ...but an invalid byte after a partial prefix is not.
    ([b'"\xe4', b'\xff', b'"'], common.InvalidUTF8Error),
    # ...and a prefix left incomplete by the end of the input is truncation.
    ([b'"\xe4\xb8'], common.IncompleteJSONError),
    ([b'"\xff'], common.InvalidUTF8Error),
    ([b'"\xe4\xb8\x96', b'\n"'], common.InvalidControlCharacterError),
])
def test_utf8_and_control_characters_across_chunks(content, expected):
    if expected is None:
        list(BACKEND.basic_parse_gen(ChunkedFile(content)))
        return
    with pytest.raises(expected):
        list(BACKEND.basic_parse_gen(ChunkedFile(content)))


def test_errors_reach_parse_and_items():
    """The typed errors are not swallowed by the higher level interfaces."""
    for routine, args in ((BACKEND.parse, ()),
                          (BACKEND.items, ('',)),
                          (BACKEND.kvitems, ('',))):
        with pytest.raises(common.InvalidControlCharacterError):
            list(routine(io.BytesIO(b'[1, "a\tb"]'), *args))


@pytest.mark.parametrize('content, expected, lexical, incomplete', [
    # The three malformed-content shapes: control character, escape, UTF-8.
    (b'"a\nb"', common.InvalidControlCharacterError, True, False),
    (b'"a\tb"', common.InvalidControlCharacterError, True, False),
    (br'"\q"', common.InvalidEscapeSequenceError, True, False),
    (br'"\u12G4"', common.InvalidEscapeSequenceError, True, False),
    (b'"\xff"', common.InvalidUTF8Error, True, False),
    # Truncation must stay out of the lexical family.
    (b'"unterminated', common.IncompleteJSONError, False, True),
    (b'"\xc3', common.IncompleteJSONError, False, True),
    # Other lexical errors keep their exact type.
    (b'+1', common.JSONError, False, False),
    (b'01', common.JSONError, False, False),
])
def test_three_shapes_contrast(content, expected, lexical, incomplete):
    """One table showing how each shape is classified, for easier comparison."""
    with pytest.raises(common.JSONError) as excinfo:
        _run_gen(content)
    error = excinfo.value
    assert type(error) is expected
    assert isinstance(error, common.IncompleteJSONError) is incomplete
    assert isinstance(error, common.JSONLexicalError) is lexical

