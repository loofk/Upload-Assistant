package rtorrent

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
)

const maxResponseBytes = 32 << 20

var methodPattern = regexp.MustCompile(`^[A-Za-z0-9_.]+$`)

type Config struct {
	Endpoint    string
	Timeout     time.Duration
	Credentials map[string]string
	HTTPClient  *http.Client
}

type rpcClient struct {
	endpoint    *url.URL
	httpClient  *http.Client
	credentials map[string]string
}

type faultError struct {
	Code    int64
	Message string
}

func (err *faultError) Error() string {
	return fmt.Sprintf("rTorrent XML-RPC fault %d: %s", err.Code, safeMessage(err.Message))
}

type xmlText struct {
	Text string `xml:",chardata"`
}

type xmlArray struct {
	Values []xmlValue `xml:"data>value"`
}

type xmlMember struct {
	Name  string   `xml:"name"`
	Value xmlValue `xml:"value"`
}

type xmlStruct struct {
	Members []xmlMember `xml:"member"`
}

type xmlValue struct {
	Text    string     `xml:",chardata"`
	String  *xmlText   `xml:"string"`
	Int     *xmlText   `xml:"int"`
	I4      *xmlText   `xml:"i4"`
	I8      *xmlText   `xml:"i8"`
	Boolean *xmlText   `xml:"boolean"`
	Double  *xmlText   `xml:"double"`
	Base64  *xmlText   `xml:"base64"`
	Array   *xmlArray  `xml:"array"`
	Struct  *xmlStruct `xml:"struct"`
	Nil     *struct{}  `xml:"nil"`
}

type xmlParam struct {
	Value xmlValue `xml:"value"`
}

type methodResponse struct {
	XMLName xml.Name   `xml:"methodResponse"`
	Params  []xmlParam `xml:"params>param"`
	Fault   *xmlParam  `xml:"fault"`
}

func newRPCClient(config Config) (*rpcClient, error) {
	endpoint, err := url.Parse(strings.TrimSpace(config.Endpoint))
	if err != nil || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Host == "" {
		return nil, fmt.Errorf("invalid rTorrent XML-RPC endpoint")
	}
	if endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return nil, fmt.Errorf("rTorrent endpoint must not contain credentials, query, or fragment")
	}
	if config.Timeout <= 0 {
		config.Timeout = 30 * time.Second
	}
	username := strings.TrimSpace(config.Credentials["username"])
	password := config.Credentials["password"]
	if (username == "") != (password == "") {
		return nil, fmt.Errorf("%w: rTorrent username and password must be supplied together", qbittorrent.ErrUnauthorized)
	}
	if config.HTTPClient == nil {
		config.HTTPClient = &http.Client{
			Timeout: config.Timeout,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return errors.New("rTorrent endpoint redirect is not allowed")
			},
		}
	}
	return &rpcClient{endpoint: endpoint, httpClient: config.HTTPClient, credentials: config.Credentials}, nil
}

func (client *rpcClient) call(ctx context.Context, method string, params ...any) (any, error) {
	if !methodPattern.MatchString(method) {
		return nil, fmt.Errorf("invalid rTorrent XML-RPC method")
	}
	var payload bytes.Buffer
	payload.WriteString(`<?xml version="1.0"?><methodCall><methodName>`)
	writeEscaped(&payload, method)
	payload.WriteString(`</methodName><params>`)
	for _, param := range params {
		payload.WriteString(`<param>`)
		if err := encodeValue(&payload, param); err != nil {
			return nil, fmt.Errorf("encode rTorrent %s request: %w", method, err)
		}
		payload.WriteString(`</param>`)
	}
	payload.WriteString(`</params></methodCall>`)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, client.endpoint.String(), bytes.NewReader(payload.Bytes()))
	if err != nil {
		return nil, fmt.Errorf("create rTorrent request: %w", err)
	}
	request.Header.Set("Accept", "text/xml")
	request.Header.Set("Content-Type", "text/xml")
	request.Header.Set("User-Agent", "Upload-Assistant/2")
	if client.credentials["username"] != "" {
		request.SetBasicAuth(client.credentials["username"], client.credentials["password"])
	}
	response, err := client.httpClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("rTorrent request failed: %w", err)
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	_ = response.Body.Close()
	if readErr != nil {
		return nil, fmt.Errorf("read rTorrent response: %w", readErr)
	}
	if len(body) > maxResponseBytes {
		return nil, fmt.Errorf("rTorrent response exceeds %d bytes", maxResponseBytes)
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return nil, qbittorrent.ErrUnauthorized
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("rTorrent XML-RPC returned HTTP %d: %s", response.StatusCode, safeMessage(string(body)))
	}
	lowerBody := bytes.ToLower(body)
	if bytes.Contains(lowerBody, []byte("<!doctype")) || bytes.Contains(lowerBody, []byte("<!entity")) {
		return nil, fmt.Errorf("rTorrent XML-RPC response contains a prohibited document declaration")
	}
	decoder := xml.NewDecoder(bytes.NewReader(body))
	decoder.Strict = true
	var decoded methodResponse
	if err := decoder.Decode(&decoded); err != nil {
		return nil, fmt.Errorf("decode rTorrent %s response: %w", method, err)
	}
	if decoded.Fault != nil {
		value, err := decodeValue(decoded.Fault.Value)
		if err != nil {
			return nil, fmt.Errorf("decode rTorrent %s fault: %w", method, err)
		}
		fault, ok := value.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("decode rTorrent %s fault: invalid fault structure", method)
		}
		code, ok := fault["faultCode"].(int64)
		if !ok {
			return nil, fmt.Errorf("decode rTorrent %s fault: invalid fault code", method)
		}
		message, ok := fault["faultString"].(string)
		if !ok {
			return nil, fmt.Errorf("decode rTorrent %s fault: invalid fault message", method)
		}
		return nil, &faultError{Code: code, Message: message}
	}
	if len(decoded.Params) != 1 {
		return nil, fmt.Errorf("rTorrent %s response must contain exactly one parameter", method)
	}
	value, err := decodeValue(decoded.Params[0].Value)
	if err != nil {
		return nil, fmt.Errorf("decode rTorrent %s value: %w", method, err)
	}
	return value, nil
}

func encodeValue(buffer *bytes.Buffer, value any) error {
	buffer.WriteString(`<value>`)
	switch typed := value.(type) {
	case string:
		buffer.WriteString(`<string>`)
		writeEscaped(buffer, typed)
		buffer.WriteString(`</string>`)
	case []byte:
		buffer.WriteString(`<base64>`)
		buffer.WriteString(base64.StdEncoding.EncodeToString(typed))
		buffer.WriteString(`</base64>`)
	case bool:
		if typed {
			buffer.WriteString(`<boolean>1</boolean>`)
		} else {
			buffer.WriteString(`<boolean>0</boolean>`)
		}
	case int:
		buffer.WriteString(`<i8>` + strconv.FormatInt(int64(typed), 10) + `</i8>`)
	case int64:
		buffer.WriteString(`<i8>` + strconv.FormatInt(typed, 10) + `</i8>`)
	case []string:
		buffer.WriteString(`<array><data>`)
		for _, item := range typed {
			if err := encodeValue(buffer, item); err != nil {
				return err
			}
		}
		buffer.WriteString(`</data></array>`)
	case []any:
		buffer.WriteString(`<array><data>`)
		for _, item := range typed {
			if err := encodeValue(buffer, item); err != nil {
				return err
			}
		}
		buffer.WriteString(`</data></array>`)
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		buffer.WriteString(`<struct>`)
		for _, key := range keys {
			buffer.WriteString(`<member><name>`)
			writeEscaped(buffer, key)
			buffer.WriteString(`</name>`)
			if err := encodeValue(buffer, typed[key]); err != nil {
				return err
			}
			buffer.WriteString(`</member>`)
		}
		buffer.WriteString(`</struct>`)
	default:
		return fmt.Errorf("unsupported XML-RPC value type %T", value)
	}
	buffer.WriteString(`</value>`)
	return nil
}

func decodeValue(value xmlValue) (any, error) {
	return decodeValueDepth(value, 0)
}

func decodeValueDepth(value xmlValue, depth int) (any, error) {
	if depth > 64 {
		return nil, fmt.Errorf("XML-RPC value nesting exceeds 64 levels")
	}
	switch {
	case value.String != nil:
		return value.String.Text, nil
	case value.Int != nil:
		return strconv.ParseInt(strings.TrimSpace(value.Int.Text), 10, 64)
	case value.I4 != nil:
		return strconv.ParseInt(strings.TrimSpace(value.I4.Text), 10, 64)
	case value.I8 != nil:
		return strconv.ParseInt(strings.TrimSpace(value.I8.Text), 10, 64)
	case value.Boolean != nil:
		switch strings.TrimSpace(value.Boolean.Text) {
		case "0":
			return false, nil
		case "1":
			return true, nil
		default:
			return nil, fmt.Errorf("invalid XML-RPC boolean")
		}
	case value.Double != nil:
		return strconv.ParseFloat(strings.TrimSpace(value.Double.Text), 64)
	case value.Base64 != nil:
		return base64.StdEncoding.DecodeString(strings.TrimSpace(value.Base64.Text))
	case value.Array != nil:
		result := make([]any, len(value.Array.Values))
		for index, item := range value.Array.Values {
			decoded, err := decodeValueDepth(item, depth+1)
			if err != nil {
				return nil, err
			}
			result[index] = decoded
		}
		return result, nil
	case value.Struct != nil:
		result := make(map[string]any, len(value.Struct.Members))
		for _, member := range value.Struct.Members {
			decoded, err := decodeValueDepth(member.Value, depth+1)
			if err != nil {
				return nil, err
			}
			result[member.Name] = decoded
		}
		return result, nil
	case value.Nil != nil:
		return nil, nil
	default:
		return strings.TrimSpace(value.Text), nil
	}
}

func writeEscaped(buffer *bytes.Buffer, value string) {
	_ = xml.EscapeText(buffer, []byte(value))
}

func asInt64(value any) (int64, bool) {
	switch typed := value.(type) {
	case int64:
		return typed, true
	case float64:
		return int64(typed), true
	default:
		return 0, false
	}
}

func safeMessage(message string) string {
	message = strings.TrimSpace(strings.NewReplacer("\r", " ", "\n", " ", "\t", " ").Replace(message))
	if len(message) > 300 {
		message = message[:300]
	}
	return message
}
