package legacy

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
	"unicode/utf8"
)

const (
	maxConfigBytes = 8 << 20
	maxParseDepth  = 64
	maxParseNodes  = 1_000_000
)

type tokenKind uint8

const (
	tokenEOF tokenKind = iota
	tokenIdentifier
	tokenString
	tokenNumber
	tokenPunctuation
)

type token struct {
	kind         tokenKind
	value        string
	line, column int
}

type literalParser struct {
	lexer *literalLexer
	next  token
	nodes int
}

// ParseConfigLiteral parses a deliberately small, non-executable Python
// literal subset. It never imports, evaluates, or invokes Python code.
func ParseConfigLiteral(body []byte) (map[string]any, error) {
	if len(body) == 0 || len(body) > maxConfigBytes {
		return nil, fmt.Errorf("legacy config must be between 1 byte and %d bytes", maxConfigBytes)
	}
	lexer := &literalLexer{source: string(body), line: 1, column: 1}
	parser := &literalParser{lexer: lexer}
	if err := parser.advance(); err != nil {
		return nil, err
	}
	if parser.next.kind != tokenIdentifier || parser.next.value != "config" {
		return nil, parser.errorf("file must contain only a top-level config assignment")
	}
	if err := parser.advance(); err != nil {
		return nil, err
	}
	if parser.next.kind != tokenPunctuation || parser.next.value != "=" {
		return nil, parser.errorf("config must be assigned with '='")
	}
	if err := parser.advance(); err != nil {
		return nil, err
	}
	value, err := parser.parseValue(0)
	if err != nil {
		return nil, err
	}
	root, ok := value.(map[string]any)
	if !ok {
		return nil, parser.errorf("config value must be a dictionary")
	}
	if parser.next.kind != tokenEOF {
		return nil, parser.errorf("executable statements or extra expressions after config are not allowed")
	}
	return root, nil
}

func (parser *literalParser) parseValue(depth int) (any, error) {
	if depth > maxParseDepth {
		return nil, parser.errorf(fmt.Sprintf("literal nesting exceeds %d levels", maxParseDepth))
	}
	parser.nodes++
	if parser.nodes > maxParseNodes {
		return nil, parser.errorf("literal contains too many values")
	}
	current := parser.next
	switch current.kind {
	case tokenString:
		if err := parser.advance(); err != nil {
			return nil, err
		}
		return current.value, nil
	case tokenNumber:
		if err := parser.advance(); err != nil {
			return nil, err
		}
		if strings.ContainsAny(current.value, ".eE") {
			value, err := strconv.ParseFloat(current.value, 64)
			if err != nil {
				return nil, parser.errorf("invalid numeric literal")
			}
			return value, nil
		}
		value, err := strconv.ParseInt(current.value, 10, 64)
		if err != nil {
			return nil, parser.errorf("integer literal is out of range")
		}
		return value, nil
	case tokenIdentifier:
		if err := parser.advance(); err != nil {
			return nil, err
		}
		switch current.value {
		case "True":
			return true, nil
		case "False":
			return false, nil
		case "None":
			return nil, nil
		default:
			return nil, parser.errorAt(current, "only True, False, and None identifiers are allowed as values")
		}
	case tokenPunctuation:
		switch current.value {
		case "{":
			return parser.parseDictionary(depth + 1)
		case "[":
			return parser.parseSequence(depth+1, "]")
		case "(":
			return parser.parseSequence(depth+1, ")")
		}
	}
	return nil, parser.errorf("expected a Python literal value")
}

func (parser *literalParser) parseDictionary(depth int) (map[string]any, error) {
	if err := parser.advance(); err != nil { // consume {
		return nil, err
	}
	result := map[string]any{}
	if parser.isPunctuation("}") {
		_ = parser.advance()
		return result, nil
	}
	for {
		keyToken := parser.next
		if keyToken.kind != tokenString {
			return nil, parser.errorf("dictionary keys must be quoted strings")
		}
		key := keyToken.value
		if _, exists := result[key]; exists {
			return nil, parser.errorAt(keyToken, "duplicate dictionary key is not allowed")
		}
		if err := parser.advance(); err != nil {
			return nil, err
		}
		if !parser.isPunctuation(":") {
			return nil, parser.errorf("dictionary key must be followed by ':'")
		}
		if err := parser.advance(); err != nil {
			return nil, err
		}
		value, err := parser.parseValue(depth)
		if err != nil {
			return nil, err
		}
		result[key] = value
		if parser.isPunctuation("}") {
			_ = parser.advance()
			return result, nil
		}
		if !parser.isPunctuation(",") {
			return nil, parser.errorf("dictionary entries must be separated by ','")
		}
		if err := parser.advance(); err != nil {
			return nil, err
		}
		if parser.isPunctuation("}") {
			_ = parser.advance()
			return result, nil
		}
	}
}

func (parser *literalParser) parseSequence(depth int, closing string) ([]any, error) {
	if err := parser.advance(); err != nil { // consume opener
		return nil, err
	}
	result := []any{}
	if parser.isPunctuation(closing) {
		_ = parser.advance()
		return result, nil
	}
	for {
		value, err := parser.parseValue(depth)
		if err != nil {
			return nil, err
		}
		result = append(result, value)
		if parser.isPunctuation(closing) {
			_ = parser.advance()
			return result, nil
		}
		if !parser.isPunctuation(",") {
			return nil, parser.errorf("sequence values must be separated by ','")
		}
		if err := parser.advance(); err != nil {
			return nil, err
		}
		if parser.isPunctuation(closing) {
			_ = parser.advance()
			return result, nil
		}
	}
}

func (parser *literalParser) advance() error {
	next, err := parser.lexer.nextToken()
	if err != nil {
		return err
	}
	parser.next = next
	return nil
}

func (parser *literalParser) isPunctuation(value string) bool {
	return parser.next.kind == tokenPunctuation && parser.next.value == value
}

func (parser *literalParser) errorf(message string) error {
	return parser.errorAt(parser.next, message)
}

func (parser *literalParser) errorAt(value token, message string) error {
	return fmt.Errorf("legacy config line %d column %d: %s", value.line, value.column, message)
}

type literalLexer struct {
	source       string
	offset       int
	line, column int
}

func (lexer *literalLexer) nextToken() (token, error) {
	lexer.skipSpaceAndComments()
	result := token{line: lexer.line, column: lexer.column}
	if lexer.offset >= len(lexer.source) {
		result.kind = tokenEOF
		return result, nil
	}
	current := lexer.source[lexer.offset]
	if current == '\'' || current == '"' {
		value, err := lexer.scanString(current)
		if err != nil {
			return token{}, err
		}
		result.kind, result.value = tokenString, value
		return result, nil
	}
	if isIdentifierStart(current) {
		start := lexer.offset
		for lexer.offset < len(lexer.source) && isIdentifierContinue(lexer.source[lexer.offset]) {
			lexer.consumeByte()
		}
		result.kind, result.value = tokenIdentifier, lexer.source[start:lexer.offset]
		return result, nil
	}
	if isNumberStart(lexer.source, lexer.offset) {
		start := lexer.offset
		if current == '+' || current == '-' {
			lexer.consumeByte()
		}
		for lexer.offset < len(lexer.source) && isNumberByte(lexer.source[lexer.offset]) {
			lexer.consumeByte()
		}
		result.kind, result.value = tokenNumber, lexer.source[start:lexer.offset]
		return result, nil
	}
	if strings.ContainsRune("{}[]():,=", rune(current)) {
		lexer.consumeByte()
		result.kind, result.value = tokenPunctuation, string(current)
		return result, nil
	}
	return token{}, fmt.Errorf("legacy config line %d column %d: unsupported token", lexer.line, lexer.column)
}

func (lexer *literalLexer) skipSpaceAndComments() {
	for lexer.offset < len(lexer.source) {
		switch lexer.source[lexer.offset] {
		case ' ', '\t', '\r', '\n':
			lexer.consumeByte()
		case '#':
			for lexer.offset < len(lexer.source) && lexer.source[lexer.offset] != '\n' {
				lexer.consumeByte()
			}
		default:
			return
		}
	}
}

func (lexer *literalLexer) scanString(quote byte) (string, error) {
	startLine, startColumn := lexer.line, lexer.column
	lexer.consumeByte()
	var result strings.Builder
	for lexer.offset < len(lexer.source) {
		current := lexer.source[lexer.offset]
		if current == quote {
			lexer.consumeByte()
			return result.String(), nil
		}
		if current == '\n' || current == '\r' {
			return "", fmt.Errorf("legacy config line %d column %d: unterminated string literal", startLine, startColumn)
		}
		if current != '\\' {
			r, size := utf8.DecodeRuneInString(lexer.source[lexer.offset:])
			if r == utf8.RuneError && size == 1 {
				return "", fmt.Errorf("legacy config line %d column %d: invalid UTF-8", lexer.line, lexer.column)
			}
			result.WriteRune(r)
			for range size {
				lexer.consumeByte()
			}
			continue
		}
		lexer.consumeByte()
		if lexer.offset >= len(lexer.source) {
			break
		}
		escaped := lexer.source[lexer.offset]
		lexer.consumeByte()
		switch escaped {
		case '\n':
			continue
		case '\\', '\'', '"':
			result.WriteByte(escaped)
		case 'n':
			result.WriteByte('\n')
		case 'r':
			result.WriteByte('\r')
		case 't':
			result.WriteByte('\t')
		case 'b':
			result.WriteByte('\b')
		case 'f':
			result.WriteByte('\f')
		case 'v':
			result.WriteByte('\v')
		case 'a':
			result.WriteByte('\a')
		case 'x':
			r, err := lexer.scanHexEscape(2)
			if err != nil {
				return "", err
			}
			result.WriteRune(r)
		case 'u':
			r, err := lexer.scanHexEscape(4)
			if err != nil {
				return "", err
			}
			result.WriteRune(r)
		case 'U':
			r, err := lexer.scanHexEscape(8)
			if err != nil || !utf8.ValidRune(r) {
				return "", fmt.Errorf("legacy config line %d column %d: invalid Unicode escape", lexer.line, lexer.column)
			}
			result.WriteRune(r)
		default:
			return "", fmt.Errorf("legacy config line %d column %d: unsupported string escape", lexer.line, lexer.column)
		}
	}
	return "", fmt.Errorf("legacy config line %d column %d: unterminated string literal", startLine, startColumn)
}

func (lexer *literalLexer) scanHexEscape(length int) (rune, error) {
	if lexer.offset+length > len(lexer.source) {
		return 0, errors.New("legacy config contains a truncated hexadecimal escape")
	}
	raw := lexer.source[lexer.offset : lexer.offset+length]
	value, err := strconv.ParseUint(raw, 16, 32)
	if err != nil {
		return 0, fmt.Errorf("legacy config line %d column %d: invalid hexadecimal escape", lexer.line, lexer.column)
	}
	for range length {
		lexer.consumeByte()
	}
	return rune(value), nil
}

func (lexer *literalLexer) consumeByte() {
	if lexer.offset >= len(lexer.source) {
		return
	}
	if lexer.source[lexer.offset] == '\n' {
		lexer.line++
		lexer.column = 1
	} else {
		lexer.column++
	}
	lexer.offset++
}

func isIdentifierStart(value byte) bool {
	return value == '_' || value >= 'A' && value <= 'Z' || value >= 'a' && value <= 'z'
}

func isIdentifierContinue(value byte) bool {
	return isIdentifierStart(value) || value >= '0' && value <= '9'
}

func isNumberStart(source string, offset int) bool {
	if offset >= len(source) {
		return false
	}
	value := source[offset]
	if value >= '0' && value <= '9' {
		return true
	}
	return (value == '+' || value == '-') && offset+1 < len(source) && source[offset+1] >= '0' && source[offset+1] <= '9'
}

func isNumberByte(value byte) bool {
	return value >= '0' && value <= '9' || value == '.' || value == 'e' || value == 'E' || value == '+' || value == '-'
}
