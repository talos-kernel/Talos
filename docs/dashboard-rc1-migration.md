# Dashboard request boundary in RC1

Local access through the loopback interface on port 8810 is unchanged.
Foreign Host headers and cross-origin browser reads now receive HTTP 403. Direct
non-loopback binds are refused. This closes the DNS-rebinding path to local status
data and does not add a write or approval endpoint.

If a private HTTPS proxy preserves its external Host header, set
`TALOS_DASHBOARD_ALLOWED_HOSTS` in the **dashboard process environment** to the exact
authority used by the browser, for example `dashboard.example.com` or
`dashboard.example.com:8443`. Multiple authorities are comma-separated; no wildcard
is accepted. With systemd, use a local service override with an `Environment=` line,
then reload the units and restart that dashboard service. The proxy must still
authenticate and authorize viewers. Forwarded headers are not trusted.

This optional variable is POLICY and cannot be changed with `talos config set`.
The agent service and all existing tool-approval semantics are unchanged.
