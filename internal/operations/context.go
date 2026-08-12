package operations

import "context"

type correlationKey struct{}

// Correlation is the non-secret identity carried from an HTTP request or a
// workflow attempt into logs and audit records.
type Correlation struct {
	RequestID string
	TraceID   string
	JobID     string
	StepKey   string
	AttemptID string
	ActorType string
	ActorID   string
}

func WithCorrelation(ctx context.Context, value Correlation) context.Context {
	return context.WithValue(ctx, correlationKey{}, &value)
}

func CorrelationFromContext(ctx context.Context) Correlation {
	value, _ := ctx.Value(correlationKey{}).(*Correlation)
	if value == nil {
		return Correlation{}
	}
	return *value
}

func SetActor(ctx context.Context, actorType, actorID string) {
	if value, _ := ctx.Value(correlationKey{}).(*Correlation); value != nil {
		value.ActorType, value.ActorID = actorType, actorID
	}
}
