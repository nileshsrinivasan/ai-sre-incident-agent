CREATE TABLE app_logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
  service TEXT, level TEXT, message TEXT);
CREATE INDEX ON app_logs (service, ts DESC);

CREATE TABLE incidents (
  id SERIAL PRIMARY KEY, title TEXT, service TEXT, description TEXT,
  severity TEXT, status TEXT DEFAULT 'open', summary TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE approvals (
  id SERIAL PRIMARY KEY, incident_id INT, proposed_action TEXT, target TEXT,
  risk TEXT, rationale TEXT, status TEXT DEFAULT 'pending',   -- pending|approved|rejected|executed
  approver TEXT, comment TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), decided_at TIMESTAMPTZ);

CREATE TABLE audit_log (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
  incident_id INT, actor TEXT, event TEXT, detail JSONB);

-- Knowledge base of past incidents (what "incident retrieval" searches)
CREATE TABLE past_incidents (
  id SERIAL PRIMARY KEY, title TEXT, service TEXT, symptoms TEXT,
  root_cause TEXT, resolution TEXT);
INSERT INTO past_incidents (title, service, symptoms, root_cause, resolution) VALUES
('Checkout 5xx spike after release','demo-service','error rate spike 500 NullPointerError after deploy release regression','Regression introduced in new release','rollback_deployment to previous version'),
('API latency from long report query','demo-service','slow queries latency high database lock timeout report job long running orders table','Nightly report query held locks for minutes','terminate_blocking_queries; move report job off-peak'),
('Connection pool exhausted','demo-service','connections exhausted too many clients timeout database pool','Pool too small for traffic','Increase pool size, restart_service'),
('Memory leak and OOM restarts','demo-service','memory oom killed restart latency growing','Leak in image-processing library','restart_service as stopgap, patch leak');
