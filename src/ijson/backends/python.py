'''
Pure-python parsing backend.
'''
from json.decoder import scanstring, JSONDecodeError
import re

from ijson import common, utils
import codecs


LEXEME_RE = re.compile(r'[a-z0-9eE\.\+-]+|\S')
INTEGER_RE = re.compile(r'-?[0-9]+')
UNARY_LEXEMES = set('[]{},')
EOF = -1, None


class UnexpectedSymbol(common.JSONError):
    def __init__(self, symbol, pos):
        super(UnexpectedSymbol, self).__init__(
            'Unexpected symbol %r at %d' % (symbol, pos)
        )


# Prefixes of the messages the standard library's string scanner uses. They are
# matched as prefixes because the C and the Python implementations produce
# different suffixes (and report ``JSONDecodeError.pos`` differently).
_CONTROL_CHARACTER_MSG = 'Invalid control character'
_INVALID_ESCAPE_MSG = 'Invalid \\escape'
_INVALID_U_ESCAPE_MSG = 'Invalid \\uXXXX escape'
_UNTERMINATED_STRING_MSG = 'Unterminated string'


def _is_control_character(ch):
    return ord(ch) < 0x20


def _first_unescaped_control_character(s):
    '''
    Returns the index of the first control character in the string lexeme
    ``s`` that is not part of an escape sequence, or ``None`` if there is none.

    Escape sequences never contain literal control characters (they use
    letters such as ``n``, ``t`` or ``u``), so skipping the character after a
    backslash is enough to avoid false positives.
    '''
    index = 1
    while index < len(s):
        if s[index] == '\\':
            index += 2
            continue
        if _is_control_character(s[index]):
            return index
        index += 1
    return None


def _relative_error_position(s, e):
    '''
    Returns the position, within ``s``, of the character that starts the
    offending construct: the control character itself, or the backslash that
    introduces an invalid escape sequence.

    ``JSONDecodeError.pos`` does not have that meaning consistently: the C
    implementation points at the offending character while the Python one
    points at the character after it, and for escape sequences they disagree on
    whether the backslash or the escaped character is reported. Normalizing
    here keeps ``position`` stable across Python implementations.
    '''
    if e.msg.startswith(_CONTROL_CHARACTER_MSG):
        position = _first_unescaped_control_character(s)
        if position is not None:
            return position
    pos = e.pos
    if pos < len(s) and s[pos] == '\\':
        return pos
    if pos > 0 and s[pos - 1] == '\\':
        return pos - 1
    return pos


def _translate_scanstring_error(s, e, base=None):
    '''
    Maps a `json.JSONDecodeError` raised by `scanstring` onto ijson's error
    family.

    Genuine truncation (the scanner ran out of input) keeps the historical
    `IncompleteJSONError` behavior; lexical errors that no additional input can
    fix are raised as `JSONLexicalError` subclasses so callers can route them
    to a quarantine area instead of treating them as truncated input.

    ``base`` is the offset of ``s`` within the parsed document, so that
    ``position`` and the message both refer to the document rather than to the
    string lexeme. Messages are built here instead of reusing the standard
    library's, whose line/column information is also relative to the lexeme and
    therefore misleading once the error surfaces to ijson's caller.
    '''
    message = e.msg
    if message.startswith(_UNTERMINATED_STRING_MSG):
        return common.IncompleteJSONError(e)
    relative = _relative_error_position(s, e)
    position = None if base is None else base + relative
    location = '' if position is None else ' at position %d' % (position,)
    if message.startswith(_CONTROL_CHARACTER_MSG):
        cls = common.InvalidControlCharacterError
        char = s[relative] if relative < len(s) else None
        detail = 'Invalid control character %r%s' % (char, location)
    elif message.startswith((_INVALID_ESCAPE_MSG, _INVALID_U_ESCAPE_MSG)):
        cls = common.InvalidEscapeSequenceError
        escape = s[relative:relative + 2]
        detail = 'Invalid escape sequence %r%s' % (escape, location)
    else:
        # Anything else scanstring reports is still invalid string content,
        # never a truncation; keep it inside the lexical error family.
        cls = common.JSONLexicalError
        detail = 'Invalid string content%s' % (location,)
    return cls(detail, position)


def _is_truncated_utf8(e):
    '''
    True if ``e`` describes a UTF-8 sequence that is merely cut short by the
    end of the input, as opposed to bytes that are not valid UTF-8 at all.

    This is decided structurally (the offending bytes are at the end of the
    input and are a valid incomplete sequence) instead of by matching the
    decoder's message, which implementations are free to word differently.
    '''
    sequence = e.object[e.start:e.end]
    if e.end != len(e.object):
        return False
    decoder = codecs.getincrementaldecoder('utf-8')()
    try:
        # An incomplete-but-valid prefix is buffered rather than rejected.
        decoder.decode(sequence, False)
    except UnicodeDecodeError:
        return False
    return True


def _translate_unicode_decode_error(e):
    '''
    Maps a `UnicodeDecodeError` raised by the incremental UTF-8 decoder onto
    ijson's error family.

    A sequence cut short at the end of the input is truncation and keeps being
    reported as `IncompleteJSONError`. Bytes that cannot start or continue a
    valid UTF-8 sequence will never decode, no matter what comes next, and are
    reported as `InvalidUTF8Error`.

    No document ``position`` can be reported: bytes are decoded before they are
    lexed, and the decoder only sees the current read buffer.
    '''
    if _is_truncated_utf8(e):
        return common.IncompleteJSONError(e)
    return common.InvalidUTF8Error('Invalid UTF-8 byte sequence: %s' % (e.reason,))


@utils.coroutine
def utf8_encoder(target):
    decoder = codecs.getincrementaldecoder('utf-8')()
    decode = decoder.decode
    send = target.send
    while True:
        try:
            final = False
            bdata = (yield)
        except GeneratorExit:
            final = True
            bdata = b''
        try:
            sdata = decode(bdata, final)
        except UnicodeDecodeError as e:
            try:
                target.close()
            except:
                pass
            raise _translate_unicode_decode_error(e) from e
        except:
            try:
                target.close()
            except:
                pass
            raise
        if sdata:
            send(sdata)
        elif not bdata:
            target.close()
            break

@utils.coroutine
def Lexer(target):
    """
    Parses lexemes out of the incoming content, and sends them to parse_value.
    A special EOF result is sent when the data source has been exhausted to
    give parse_value the possibility of raising custom exceptions due to missing
    content.
    """
    try:
        data = (yield)
    except GeneratorExit:
        data = ''
    buf = data
    pos = 0
    discarded = 0
    send = target.send
    while True:
        match = LEXEME_RE.search(buf, pos)
        if match:
            lexeme = match.group()
            if lexeme == '"':
                pos = match.start()
                start = pos + 1
                while True:
                    try:
                        end = buf.index('"', start)
                        escpos = end - 1
                        while buf[escpos] == '\\':
                            escpos -= 1
                        if (end - escpos) % 2 == 0:
                            start = end + 1
                        else:
                            break
                    except ValueError:
                        try:
                            data = (yield)
                        except GeneratorExit:
                            data = ''
                        if not data:
                            raise common.IncompleteJSONError('Incomplete string lexeme')
                        buf += data
                send((discarded + pos, buf[pos:end + 1]))
                pos = end + 1
            else:
                while lexeme not in UNARY_LEXEMES and match.end() == len(buf):
                    try:
                        data = (yield)
                    except GeneratorExit:
                        data = ''
                    if not data:
                        break
                    buf += data
                    match = LEXEME_RE.search(buf, pos)
                    lexeme = match.group()
                send((discarded + match.start(), lexeme))
                pos = match.end()
        else:
            # Don't ask data from an already exhausted source
            if data:
                try:
                    data = (yield)
                except GeneratorExit:
                    data = ''
            if not data:
                # Normally should raise StopIteration, but can raise
                # IncompleteJSONError too, which is the point of sending EOF
                try:
                    target.send(EOF)
                except StopIteration:
                    pass
                break
            discarded += len(buf)
            buf = data
            pos = 0


# Parsing states
_PARSE_VALUE = 0
_PARSE_ARRAY_ELEMENT_END = 1
_PARSE_OBJECT_KEY = 2
_PARSE_OBJECT_END = 3

# infinity singletons for overflow checks
inf = float("inf")
neg_inf = float("-inf")

@utils.coroutine
def parse_value(target, multivalue, use_float):
    """
    Parses results coming out of the Lexer into ijson events, which are sent to
    `target`. A stack keeps track of the type of object being parsed at the time
    (a value, and object or array -- the last two being values themselves).

    A special EOF result coming from the Lexer indicates that no more content is
    expected. This is used to check for incomplete content and raise the
    appropriate exception, which wouldn't be possible if the Lexer simply closed
    this co-routine (either explicitly via .close(), or implicitly by itself
    finishing and decreasing the only reference to the co-routine) since that
    causes a GeneratorExit exception that cannot be replaced with a custom one.
    """

    state_stack = [_PARSE_VALUE]
    pop = state_stack.pop
    push = state_stack.append
    send = target.send
    prev_pos, prev_symbol = None, None
    to_number = common.integer_or_float if use_float else common.integer_or_decimal
    while True:

        if prev_pos is None:
            pos, symbol = (yield)
            if (pos, symbol) == EOF:
                if state_stack:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                break
        else:
            pos, symbol = prev_pos, prev_symbol
            prev_pos, prev_symbol = None, None
        try:
            state = state_stack[-1]
        except IndexError:
            if multivalue:
                state = _PARSE_VALUE
                push(state)
            else:
                raise common.JSONError('Additional data found')
        assert state_stack

        if state == _PARSE_VALUE:
            # Simple, common cases
            if symbol == 'null':
                send(('null', None))
                pop()
            elif symbol == 'true':
                send(('boolean', True))
                pop()
            elif symbol == 'false':
                send(('boolean', False))
                pop()
            elif symbol[0] == '"':
                send(('string', parse_string(symbol, pos)))
                pop()
            # Array start
            elif symbol == '[':
                send(('start_array', None))
                pos, symbol = (yield)
                if (pos, symbol) == EOF:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                if symbol == ']':
                    send(('end_array', None))
                    pop()
                else:
                    prev_pos, prev_symbol = pos, symbol
                    push(_PARSE_ARRAY_ELEMENT_END)
                    push(_PARSE_VALUE)
            # Object start
            elif symbol == '{':
                send(('start_map', None))
                pos, symbol = (yield)
                if (pos, symbol) == EOF:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                if symbol == '}':
                    send(('end_map', None))
                    pop()
                else:
                    prev_pos, prev_symbol = pos, symbol
                    push(_PARSE_OBJECT_KEY)
            # A number
            else:
                # JSON numbers can't contain leading zeros
                if ((len(symbol) > 1 and symbol[0] == '0' and symbol[1] not in ('e', 'E', '.')) or
                    (len(symbol) > 2 and symbol[0:2] == '-0' and symbol[2] not in ('e', 'E', '.'))):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,))
                # JSON numbers can't start with a plus sign
                if symbol[0] == '+':
                    raise common.JSONError(f'Invalid JSON number: {symbol}')
                # Fractions need a leading digit and must be followed by a digit
                dot = symbol.find('.')
                if dot != -1 and not (symbol[dot - 1:dot].isdigit() and symbol[dot + 1:dot + 2].isdigit()):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,))
                try:
                    number = to_number(symbol)
                    if number == inf or number == neg_inf:
                        raise common.JSONError("float overflow: %s" % (symbol,))
                except:
                    if 'true'.startswith(symbol) or 'false'.startswith(symbol) or 'null'.startswith(symbol):
                        raise common.IncompleteJSONError('Incomplete JSON content')
                    # a well-formed integer that int() still refused is one
                    # longer than sys.get_int_max_str_digits(); let that
                    # error through as the other backends do
                    if INTEGER_RE.fullmatch(symbol):
                        raise
                    raise UnexpectedSymbol(symbol, pos)
                else:
                    send(('number', number))
                    pop()

        elif state == _PARSE_OBJECT_KEY:
            if symbol[0] != '"':
                raise UnexpectedSymbol(symbol, pos)
            send(('map_key', parse_string(symbol, pos)))
            pos, symbol = (yield)
            if (pos, symbol) == EOF:
                raise common.IncompleteJSONError('Incomplete JSON content')
            if symbol != ':':
                raise UnexpectedSymbol(symbol, pos)
            state_stack[-1] = _PARSE_OBJECT_END
            push(_PARSE_VALUE)

        elif state == _PARSE_OBJECT_END:
            if symbol == ',':
                state_stack[-1] = _PARSE_OBJECT_KEY
            elif symbol != '}':
                raise UnexpectedSymbol(symbol, pos)
            else:
                send(('end_map', None))
                pop()
                pop()

        elif state == _PARSE_ARRAY_ELEMENT_END:
            if symbol == ',':
                state_stack[-1] = _PARSE_ARRAY_ELEMENT_END
                push(_PARSE_VALUE)
            elif symbol != ']':
                raise UnexpectedSymbol(symbol, pos)
            else:
                send(('end_array', None))
                pop()
                pop()


def parse_string(symbol, pos=None):
    '''
    Decodes a string lexeme (including its surrounding quotes).

    Invalid string content raises a `common.JSONLexicalError` subclass;
    truncated content keeps raising `common.IncompleteJSONError`.
    '''
    try:
        return scanstring(symbol, 1)[0]
    except JSONDecodeError as e:
        raise _translate_scanstring_error(symbol, e, base=pos) from e


def basic_parse_basecoro(target, multiple_values=False, allow_comments=False,
                         use_float=False):
    '''
    Iterator yielding unprefixed events.

    Parameters:

    - file: a readable file-like object with JSON input
    '''
    if allow_comments:
        raise ValueError("Comments are not supported by the python backend")
    return utf8_encoder(Lexer(parse_value(target, multiple_values, use_float)))


common.enrich_backend(globals(), c_comments=False)
