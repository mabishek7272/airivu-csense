package telemetry

import (
	"time"

	"github.com/gin-gonic/gin"
)

type TelemetryData struct {
	Route      string    `json:"route"`
	APIVersion string    `json:"apiVersion"`
	Timestamp  time.Time `json:"timestamp"`
}

type telemetryService struct{}

func (t *telemetryService) TelemetryMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		route := c.FullPath()
		go SendTelemetry(route)
		c.Next()
	}
}

type TelemetryService interface {
	TelemetryMiddleware() gin.HandlerFunc
}

// SendTelemetry is intentionally a no-op in this build.
//
// Upstream posts {route, apiVersion, timestamp} to log.evolution-api.com on every single
// API request. It carries no message content or customer data, but it is an outbound call
// per request from a service that runs inside a customer's network - unacceptable for
// on-prem or air-gapped deployments, and not disableable by configuration upstream.
// Apache 2.0 permits the modification. See CLARIFICATIONS.md #23.
//
// The middleware and signature are kept so the call sites upstream remain untouched,
// which keeps the diff against future upstream versions small.
func SendTelemetry(route string) {
	_ = route
}

func NewTelemetryService() TelemetryService {
	return &telemetryService{}
}
