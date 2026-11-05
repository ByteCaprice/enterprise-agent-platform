-- Enterprise AI Platform initial schema (0001_platform).
-- Frozen initialization baseline: future schema changes use new incremental migrations.

CREATE TABLE ai_api_key_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	api_key TEXT NOT NULL,
	scopes JSONB NOT NULL,
	enabled BOOLEAN NOT NULL,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_api_key_config_name UNIQUE (name),
	CONSTRAINT uk_ai_api_key_config_key UNIQUE (api_key),
	CONSTRAINT ck_ai_api_key_name CHECK (length(name) > 0),
	CONSTRAINT ck_ai_api_key_value CHECK (length(api_key) > 0),
	CONSTRAINT ck_ai_api_key_scopes_array CHECK (jsonb_typeof(scopes) = 'array'),
	CONSTRAINT ck_ai_api_key_scopes_nonempty CHECK (jsonb_array_length(scopes) > 0)
);

CREATE TABLE ai_global_concurrency (
	key TEXT NOT NULL,
	max_running INTEGER,
	PRIMARY KEY (key),
	CONSTRAINT ck_ai_global_concurrency CHECK (key='global' AND (max_running IS NULL OR max_running BETWEEN 0 AND 100000))
);

CREATE TABLE ai_oidc_provider (
	key TEXT NOT NULL,
	issuer TEXT NOT NULL,
	audience TEXT NOT NULL,
	jwks JSONB NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (key),
	UNIQUE (issuer)
);

CREATE TABLE ai_project (
	key TEXT NOT NULL,
	name TEXT NOT NULL,
	description TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (key)
);

CREATE TABLE ai_run_log (
	id TEXT NOT NULL,
	trace_id TEXT NOT NULL,
	component TEXT NOT NULL,
	event_type TEXT NOT NULL,
	level TEXT NOT NULL,
	message TEXT,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	data JSONB NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE ai_worker_health (
	instance_id TEXT NOT NULL,
	worker_id TEXT NOT NULL,
	role TEXT NOT NULL,
	executors JSONB NOT NULL,
	workflow_agents BOOLEAN NOT NULL,
	state TEXT NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (instance_id),
	CONSTRAINT ck_ai_worker_health CHECK (role IN ('task','callback','recovery') AND state IN ('running','stopped','failed'))
);

CREATE TABLE ai_budget_cycle (
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	currency TEXT NOT NULL,
	limit_microunits BIGINT NOT NULL,
	reserve_per_call_microunits BIGINT NOT NULL,
	starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
	rates JSONB NOT NULL,
	PRIMARY KEY (project_key, key),
	CONSTRAINT ck_ai_budget_cycle CHECK (limit_microunits>0 AND reserve_per_call_microunits>0 AND ends_at>starts_at AND currency ~ '^[A-Z]{3}$'),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_callback_endpoint (
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	url TEXT NOT NULL,
	credential_ref TEXT NOT NULL,
	max_attempts INTEGER NOT NULL,
	retry_delay_seconds INTEGER NOT NULL,
	timeout_seconds INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (project_key, key),
	CONSTRAINT ck_ai_callback_endpoint CHECK (max_attempts BETWEEN 1 AND 20 AND retry_delay_seconds BETWEEN 1 AND 86400 AND timeout_seconds BETWEEN 1 AND 30),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_capability (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	kind TEXT NOT NULL,
	name TEXT NOT NULL,
	description TEXT NOT NULL,
	default_version TEXT,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_capability_project_key UNIQUE (project_key, key),
	CONSTRAINT uk_ai_capability_id_project UNIQUE (id, project_key),
	CONSTRAINT fk_ai_capability_project FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_platform_model_provider (
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	name TEXT NOT NULL,
	base_url TEXT NOT NULL,
	credential_ref TEXT NOT NULL,
	models JSONB NOT NULL,
	max_tokens INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (project_key, key),
	CONSTRAINT ck_ai_platform_model_provider_models CHECK (jsonb_typeof(models) = 'array' AND jsonb_array_length(models) BETWEEN 1 AND 100),
	CONSTRAINT ck_ai_platform_model_provider_max_tokens CHECK (max_tokens BETWEEN 1 AND 1000000),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_platform_tool_binding (
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	name TEXT NOT NULL,
	handler_key TEXT NOT NULL,
	handler_version TEXT NOT NULL,
	operation_type TEXT NOT NULL,
	max_timeout_seconds FLOAT NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (project_key, key),
	CONSTRAINT ck_ai_platform_tool_binding_operation CHECK (operation_type = 'read_only'),
	CONSTRAINT ck_ai_platform_tool_binding_timeout CHECK (max_timeout_seconds > 0 AND max_timeout_seconds <= 300),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_project_limits (
	project_key TEXT NOT NULL,
	max_queued INTEGER,
	max_running INTEGER,
	active_budget_key TEXT,
	PRIMARY KEY (project_key),
	CONSTRAINT ck_ai_project_limits CHECK ((max_queued IS NULL OR max_queued>=0) AND (max_running IS NULL OR max_running>=0)),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_quality_dataset (
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	version TEXT NOT NULL,
	owner TEXT NOT NULL,
	content_hash TEXT NOT NULL,
	cases JSONB NOT NULL,
	PRIMARY KEY (project_key, key, version),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_service_identity (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	key TEXT NOT NULL,
	name TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_service_identity_key UNIQUE (project_key, key),
	CONSTRAINT uk_ai_service_identity_project UNIQUE (id, project_key),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_capability_concurrency (
	capability_id TEXT NOT NULL,
	max_running INTEGER,
	PRIMARY KEY (capability_id),
	CONSTRAINT ck_ai_capability_concurrency CHECK (max_running IS NULL OR max_running BETWEEN 0 AND 100000),
	FOREIGN KEY(capability_id) REFERENCES ai_capability (id)
);

CREATE TABLE ai_capability_version (
	id TEXT NOT NULL,
	capability_id TEXT NOT NULL,
	version TEXT NOT NULL,
	status TEXT NOT NULL,
	content_hash TEXT NOT NULL,
	input_schema JSONB NOT NULL,
	output_schema JSONB NOT NULL,
	definition JSONB NOT NULL,
	dependencies JSONB NOT NULL,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	published_by TEXT,
	publish_time TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_capability_version UNIQUE (capability_id, version),
	FOREIGN KEY(capability_id) REFERENCES ai_capability (id)
);

CREATE TABLE ai_oidc_binding (
	provider_key TEXT NOT NULL,
	subject TEXT NOT NULL,
	project_key TEXT NOT NULL,
	identity_id TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	PRIMARY KEY (provider_key, subject),
	FOREIGN KEY(identity_id, project_key) REFERENCES ai_service_identity (id, project_key),
	FOREIGN KEY(provider_key) REFERENCES ai_oidc_provider (key)
);

CREATE TABLE ai_quality_run (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	dataset_key TEXT NOT NULL,
	dataset_version TEXT NOT NULL,
	owner TEXT NOT NULL,
	capability_key TEXT NOT NULL,
	capability_version TEXT NOT NULL,
	fingerprint TEXT NOT NULL,
	task_ids JSONB NOT NULL,
	scores JSONB NOT NULL,
	state TEXT NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(project_key, dataset_key, dataset_version) REFERENCES ai_quality_dataset (project_key, key, version)
);

CREATE TABLE ai_resource_grant (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	identity_id TEXT NOT NULL,
	action TEXT NOT NULL,
	resource_key TEXT NOT NULL,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(identity_id, project_key) REFERENCES ai_service_identity (id, project_key),
	CONSTRAINT uk_ai_resource_grant UNIQUE (identity_id, action, resource_key),
	CONSTRAINT ck_ai_resource_grant_action CHECK (action IN ('capability:read','capability:write','capability:publish','prompt:render','task:submit','task:read','task:cancel','model:invoke','tool:invoke','agent:invoke','memory:read','memory:write','quality:run','quality:review','skill:use','feedback:read','feedback:write'))
);

CREATE TABLE ai_service_access_key (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	identity_id TEXT NOT NULL,
	name TEXT NOT NULL,
	digest TEXT NOT NULL,
	revoked BOOLEAN NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_by TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(identity_id, project_key) REFERENCES ai_service_identity (id, project_key),
	CONSTRAINT ck_ai_service_key_digest CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
	UNIQUE (digest)
);

CREATE TABLE ai_task (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	capability_id TEXT NOT NULL,
	version TEXT NOT NULL,
	content_hash TEXT NOT NULL,
	business_id TEXT,
	customer_id TEXT,
	submitted_by TEXT NOT NULL,
	idempotency_digest TEXT NOT NULL,
	request_fingerprint TEXT NOT NULL,
	input JSONB NOT NULL,
	status TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	result JSONB,
	error_code TEXT,
	cancel_request_id TEXT,
	cancel_requested_at TIMESTAMP WITH TIME ZONE,
	cancel_requested_by TEXT,
	event_sequence BIGINT DEFAULT 0 NOT NULL,
	event_history_complete BOOLEAN DEFAULT true NOT NULL,
	origin_task_id TEXT,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_task_origin FOREIGN KEY(origin_task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT fk_ai_task_project_capability FOREIGN KEY(capability_id, project_key) REFERENCES ai_capability (id, project_key),
	CONSTRAINT fk_ai_task_version FOREIGN KEY(capability_id, version) REFERENCES ai_capability_version (capability_id, version),
	CONSTRAINT uk_ai_task_idempotency UNIQUE (project_key, submitted_by, idempotency_digest),
	CONSTRAINT uk_ai_task_id_project UNIQUE (id, project_key),
	CONSTRAINT ck_ai_task_status CHECK (status IN ('queued','running','succeeded','failed','timed_out','cancelled')),
	CONSTRAINT ck_ai_task_finish CHECK ((status IN ('queued','running') AND finish_time IS NULL) OR (status IN ('cancelled','succeeded','failed','timed_out') AND finish_time IS NOT NULL AND finish_time >= create_time)),
	CONSTRAINT ck_ai_task_error_code CHECK ((status IN ('failed','timed_out') AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$') OR (status NOT IN ('failed','timed_out') AND error_code IS NULL)),
	CONSTRAINT ck_ai_task_result CHECK ((status = 'succeeded' AND result IS NOT NULL AND jsonb_typeof(result) = 'object') OR (status <> 'succeeded' AND result IS NULL)),
	CONSTRAINT ck_ai_task_hashes CHECK (idempotency_digest ~ '^[0-9a-f]{64}$' AND request_fingerprint ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_ai_task_input CHECK (jsonb_typeof(input) = 'object'),
	CONSTRAINT ck_ai_task_cancellation CHECK ((cancel_request_id IS NULL AND cancel_requested_at IS NULL AND cancel_requested_by IS NULL) OR (cancel_request_id IS NOT NULL AND cancel_request_id ~ '^[0-9a-f]{32}$' AND cancel_requested_at IS NOT NULL AND cancel_requested_at >= create_time AND cancel_requested_by IS NOT NULL AND length(cancel_requested_by) BETWEEN 1 AND 200 AND status IN ('running','cancelled','failed','timed_out') AND (finish_time IS NULL OR finish_time >= cancel_requested_at))),
	CONSTRAINT ck_ai_task_cancelled_intent CHECK (status != 'cancelled' OR cancel_request_id IS NOT NULL),
	CONSTRAINT ck_ai_task_event_sequence CHECK (event_sequence BETWEEN 0 AND 9007199254740991)
);

CREATE TABLE ai_gateway_request (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	submitted_by TEXT NOT NULL,
	operation TEXT NOT NULL,
	delivery_mode TEXT NOT NULL,
	task_created BOOLEAN NOT NULL,
	correlation_id TEXT,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_gateway_request_task_project FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT ck_ai_gateway_request_id CHECK (id ~ '^[0-9a-f]{32}$'),
	CONSTRAINT ck_ai_gateway_request_project_key CHECK (project_key ~ '^[a-z][a-z0-9_.-]{0,99}$'),
	CONSTRAINT ck_ai_gateway_request_task_id CHECK (length(task_id) BETWEEN 1 AND 200),
	CONSTRAINT ck_ai_gateway_request_submitted_by CHECK (length(submitted_by) BETWEEN 1 AND 256),
	CONSTRAINT ck_ai_gateway_request_operation CHECK (operation IN ('submit','execute','recover')),
	CONSTRAINT ck_ai_gateway_request_delivery_mode CHECK (delivery_mode IN ('sync','async')),
	CONSTRAINT ck_ai_gateway_request_sync_execute CHECK (operation = 'execute' OR delivery_mode = 'async'),
	CONSTRAINT ck_ai_gateway_request_correlation_id CHECK (correlation_id IS NULL OR correlation_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
);

CREATE TABLE ai_memory_fact (
	project_key TEXT NOT NULL,
	owner TEXT NOT NULL,
	subject_key TEXT NOT NULL,
	fact_key TEXT NOT NULL,
	revision INTEGER NOT NULL,
	capability_key TEXT NOT NULL,
	value JSONB,
	state TEXT NOT NULL,
	source_task_id TEXT,
	expires_at TIMESTAMP WITH TIME ZONE,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (project_key, owner, subject_key, fact_key, revision, capability_key),
	FOREIGN KEY(source_task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT ck_ai_memory_fact CHECK (revision>0 AND state IN ('active','deleted')),
	FOREIGN KEY(project_key) REFERENCES ai_project (key)
);

CREATE TABLE ai_task_attempt (
	id TEXT NOT NULL,
	task_id TEXT NOT NULL,
	number INTEGER NOT NULL,
	worker_id TEXT NOT NULL,
	token TEXT NOT NULL,
	status TEXT NOT NULL,
	start_time TIMESTAMP WITH TIME ZONE NOT NULL,
	heartbeat_time TIMESTAMP WITH TIME ZONE NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	error_code TEXT,
	cancel_observed_at TIMESTAMP WITH TIME ZONE,
	cancel_acknowledged_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_task_attempt_task FOREIGN KEY(task_id) REFERENCES ai_task (id),
	CONSTRAINT uk_ai_task_attempt_number UNIQUE (task_id, number),
	CONSTRAINT uk_ai_task_attempt_task_id UNIQUE (task_id, id),
	CONSTRAINT ck_ai_task_attempt_identity CHECK (number > 0 AND length(worker_id) BETWEEN 1 AND 200 AND token ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_ai_task_attempt_status CHECK (status IN ('running','succeeded','failed','timed_out','cancelled')),
	CONSTRAINT ck_ai_task_attempt_finish CHECK ((status = 'running' AND finish_time IS NULL) OR (status <> 'running' AND finish_time IS NOT NULL AND finish_time >= start_time)),
	CONSTRAINT ck_ai_task_attempt_times CHECK (lease_expires_at >= start_time AND heartbeat_time >= start_time),
	CONSTRAINT ck_ai_task_attempt_error_code CHECK ((status IN ('failed','timed_out') AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$') OR (status IN ('running','succeeded','cancelled') AND error_code IS NULL)),
	CONSTRAINT ck_ai_task_attempt_cancellation CHECK ((cancel_observed_at IS NULL OR cancel_observed_at >= start_time) AND (cancel_acknowledged_at IS NULL OR (cancel_observed_at IS NOT NULL AND cancel_acknowledged_at >= cancel_observed_at AND status='cancelled' AND cancel_acknowledged_at=finish_time)) AND (status != 'cancelled' OR cancel_acknowledged_at IS NOT NULL))
);

CREATE TABLE ai_task_feedback (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	submitter TEXT NOT NULL,
	revision INTEGER NOT NULL,
	request_digest TEXT NOT NULL,
	fingerprint TEXT NOT NULL,
	adoption TEXT NOT NULL,
	final_result JSONB,
	reason TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT uk_ai_task_feedback_revision UNIQUE (task_id, submitter, revision),
	CONSTRAINT uk_ai_task_feedback_request UNIQUE (task_id, submitter, request_digest),
	CONSTRAINT ck_ai_task_feedback_identity CHECK (revision > 0 AND request_digest ~ '^[0-9a-f]{64}$' AND fingerprint ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_ai_task_feedback_outcome CHECK ((adoption='modified' AND final_result IS NOT NULL AND jsonb_typeof(final_result)='object') OR (adoption IN ('adopted','not_adopted') AND final_result IS NULL))
);

CREATE TABLE ai_task_review (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	reviewer TEXT NOT NULL,
	verdict TEXT NOT NULL,
	comment TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT ck_ai_task_review CHECK (verdict IN ('approved','rejected','needs_review'))
);

CREATE TABLE ai_task_event (
	task_id TEXT NOT NULL,
	sequence BIGINT NOT NULL,
	project_key TEXT NOT NULL,
	task_attempt_id TEXT,
	entity_kind TEXT NOT NULL,
	entity_id TEXT NOT NULL,
	event_type TEXT NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (task_id, sequence),
	CONSTRAINT fk_ai_task_event_task FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT fk_ai_task_event_attempt FOREIGN KEY(task_id, task_attempt_id) REFERENCES ai_task_attempt (task_id, id),
	CONSTRAINT ck_ai_task_event_sequence CHECK (sequence BETWEEN 1 AND 9007199254740991),
	CONSTRAINT ck_ai_task_event_kind CHECK (entity_kind IN ('task','workflow_node','agent_execution','model_call','tool_call')),
	CONSTRAINT ck_ai_task_event_type CHECK (length(entity_id) BETWEEN 1 AND 200 AND event_type ~ '^[a-z][a-z0-9_]{0,99}$')
);

CREATE TABLE ai_workflow_node (
	id TEXT NOT NULL,
	task_id TEXT NOT NULL,
	task_attempt_id TEXT NOT NULL,
	node_key TEXT NOT NULL,
	kind TEXT NOT NULL,
	status TEXT NOT NULL,
	input_hash TEXT NOT NULL,
	output_hash TEXT,
	output JSONB,
	reused_from_node_id TEXT,
	start_time TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	error_code TEXT,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_workflow_node_attempt FOREIGN KEY(task_id, task_attempt_id) REFERENCES ai_task_attempt (task_id, id),
	CONSTRAINT uk_ai_workflow_node_attempt_key UNIQUE (task_attempt_id, node_key),
	CONSTRAINT uk_ai_workflow_node_task_attempt_id UNIQUE (task_id, task_attempt_id, id),
	CONSTRAINT ck_ai_workflow_node_kind CHECK (kind IN ('rule','agent','tool','llm')),
	CONSTRAINT ck_ai_workflow_node_status CHECK (status IN ('running','succeeded','failed','skipped','cancelled')),
	CONSTRAINT ck_ai_workflow_node_hashes CHECK (input_hash ~ '^[0-9a-f]{64}$' AND (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$')),
	CONSTRAINT ck_ai_workflow_node_terminal CHECK ((status = 'running' AND finish_time IS NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND finish_time IS NOT NULL AND output IS NOT NULL AND output_hash IS NOT NULL AND error_code IS NULL) OR (status = 'skipped' AND finish_time IS NOT NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output IS NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,99}$')),
	CONSTRAINT ck_ai_workflow_node_time CHECK (finish_time IS NULL OR finish_time >= start_time),
	CONSTRAINT ck_ai_workflow_node_output CHECK (output IS NULL OR jsonb_typeof(output) = 'object'),
	CONSTRAINT ck_ai_workflow_node_reused CHECK (reused_from_node_id IS NULL OR (kind='rule' AND status IN ('succeeded','skipped'))),
	CONSTRAINT ck_ai_workflow_node_key CHECK (node_key ~ '^[a-z][a-z0-9_.-]{0,99}$'),
	FOREIGN KEY(reused_from_node_id) REFERENCES ai_workflow_node (id)
);

CREATE TABLE ai_agent_execution (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	task_attempt_id TEXT NOT NULL,
	agent_capability_id TEXT NOT NULL,
	agent_key TEXT NOT NULL,
	agent_version TEXT NOT NULL,
	agent_hash TEXT NOT NULL,
	parent_node_id TEXT,
	runtime_profile TEXT NOT NULL,
	status TEXT NOT NULL,
	output_hash TEXT,
	error_code TEXT,
	start_time TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_agent_execution_task FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT fk_ai_agent_execution_attempt FOREIGN KEY(task_id, task_attempt_id) REFERENCES ai_task_attempt (task_id, id),
	CONSTRAINT fk_ai_agent_execution_node FOREIGN KEY(task_id, task_attempt_id, parent_node_id) REFERENCES ai_workflow_node (task_id, task_attempt_id, id),
	CONSTRAINT fk_ai_agent_execution_project FOREIGN KEY(agent_capability_id, project_key) REFERENCES ai_capability (id, project_key),
	CONSTRAINT fk_ai_agent_execution_version FOREIGN KEY(agent_capability_id, agent_version) REFERENCES ai_capability_version (capability_id, version),
	CONSTRAINT uk_ai_agent_execution_scope UNIQUE (project_key, task_id, task_attempt_id, id),
	CONSTRAINT uk_ai_agent_execution_node UNIQUE (parent_node_id),
	CONSTRAINT ck_ai_agent_execution_hash CHECK (agent_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_ai_agent_execution_status CHECK (status IN ('running','succeeded','failed','cancelled')),
	CONSTRAINT ck_ai_agent_execution_terminal CHECK ((status='running' AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status='succeeded' AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^AGENT_[A-Z0-9_]{1,90}$')),
	CONSTRAINT ck_ai_agent_execution_time CHECK (finish_time IS NULL OR finish_time >= start_time)
);

CREATE TABLE ai_callback_delivery (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	endpoint_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	event_sequence BIGINT NOT NULL,
	payload JSONB NOT NULL,
	state TEXT NOT NULL,
	attempts INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	lease_token TEXT,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	http_status INTEGER,
	error_code TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(project_key, endpoint_key) REFERENCES ai_callback_endpoint (project_key, key),
	FOREIGN KEY(task_id, event_sequence) REFERENCES ai_task_event (task_id, sequence),
	UNIQUE (task_id, event_sequence, endpoint_key),
	CONSTRAINT ck_ai_callback_delivery CHECK (state IN ('pending','leased','delivered','dead') AND attempts>=0 AND ((state='leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state!='leased' AND lease_token IS NULL AND lease_expires_at IS NULL)))
);

CREATE TABLE ai_agent_step (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	task_attempt_id TEXT NOT NULL,
	agent_execution_id TEXT NOT NULL,
	kind TEXT NOT NULL,
	turn INTEGER NOT NULL,
	slot INTEGER NOT NULL,
	status TEXT NOT NULL,
	resource_capability_id TEXT NOT NULL,
	resource_key TEXT NOT NULL,
	resource_version TEXT NOT NULL,
	resource_hash TEXT NOT NULL,
	source_model_step_id TEXT,
	source_model_kind TEXT,
	dispatch_id TEXT,
	tool_alias TEXT,
	source_call_id_hash TEXT,
	input_hash TEXT NOT NULL,
	output_hash TEXT,
	final_result_hash TEXT,
	decision TEXT,
	error_code TEXT,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	start_time TIMESTAMP WITH TIME ZONE,
	finish_time TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_agent_step_execution FOREIGN KEY(project_key, task_id, task_attempt_id, agent_execution_id) REFERENCES ai_agent_execution (project_key, task_id, task_attempt_id, id),
	CONSTRAINT fk_ai_agent_step_project FOREIGN KEY(resource_capability_id, project_key) REFERENCES ai_capability (id, project_key),
	CONSTRAINT fk_ai_agent_step_version FOREIGN KEY(resource_capability_id, resource_version) REFERENCES ai_capability_version (capability_id, version),
	CONSTRAINT uk_ai_agent_step_owner UNIQUE (task_id, task_attempt_id, agent_execution_id, id, kind),
	CONSTRAINT uk_ai_agent_step_source UNIQUE (agent_execution_id, turn, id, kind),
	CONSTRAINT fk_ai_agent_step_source FOREIGN KEY(agent_execution_id, turn, source_model_step_id, source_model_kind) REFERENCES ai_agent_step (agent_execution_id, turn, id, kind),
	CONSTRAINT uk_ai_agent_step_slot UNIQUE (agent_execution_id, turn, kind, slot),
	CONSTRAINT uk_ai_agent_step_source_call UNIQUE (source_model_step_id, source_call_id_hash),
	CONSTRAINT ck_ai_agent_step_kind CHECK (kind IN ('model_turn','tool_call') AND turn BETWEEN 1 AND 128 AND slot BETWEEN 0 AND 511),
	CONSTRAINT ck_ai_agent_step_source CHECK ((kind='model_turn' AND slot=0 AND source_model_step_id IS NULL AND source_model_kind IS NULL AND dispatch_id IS NULL AND tool_alias IS NULL AND source_call_id_hash IS NULL) OR (kind='tool_call' AND source_model_step_id IS NOT NULL AND source_model_kind IS NOT NULL AND source_model_kind='model_turn' AND dispatch_id IS NOT NULL AND tool_alias IS NOT NULL AND source_call_id_hash IS NOT NULL AND source_call_id_hash ~ '^[0-9a-f]{64}$')),
	CONSTRAINT ck_ai_agent_step_status CHECK (status IN ('pending','running','succeeded','failed','cancelled') AND (kind='tool_call' OR status!='pending')),
	CONSTRAINT ck_ai_agent_step_terminal CHECK ((status='pending' AND start_time IS NULL AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status='running' AND start_time IS NOT NULL AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status='succeeded' AND start_time IS NOT NULL AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^AGENT_[A-Z0-9_]{1,90}$')),
	CONSTRAINT ck_ai_agent_step_hashes CHECK (resource_hash ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_ai_agent_step_decision CHECK ((kind='tool_call' AND decision IS NULL AND final_result_hash IS NULL) OR (kind='model_turn' AND status!='succeeded' AND decision IS NULL AND final_result_hash IS NULL) OR (kind='model_turn' AND status='succeeded' AND decision IS NOT NULL AND ((decision='tools' AND final_result_hash IS NULL) OR (decision='final' AND final_result_hash IS NOT NULL AND final_result_hash ~ '^[0-9a-f]{64}$')))),
	CONSTRAINT ck_ai_agent_step_time CHECK ((start_time IS NULL OR start_time>=create_time) AND (finish_time IS NULL OR finish_time>=COALESCE(start_time,create_time))),
	UNIQUE (dispatch_id)
);

CREATE TABLE ai_platform_model_call (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	task_attempt_id TEXT NOT NULL,
	node_id TEXT,
	agent_execution_id TEXT,
	agent_step_id TEXT,
	agent_step_kind TEXT,
	policy_capability_id TEXT NOT NULL,
	policy_key TEXT NOT NULL,
	policy_version TEXT NOT NULL,
	policy_hash TEXT NOT NULL,
	provider_key TEXT NOT NULL,
	requested_model TEXT NOT NULL,
	actual_model TEXT,
	prompt_key TEXT,
	prompt_version TEXT,
	prompt_hash TEXT,
	status TEXT NOT NULL,
	input_hash TEXT NOT NULL,
	output_hash TEXT,
	prompt_tokens BIGINT,
	completion_tokens BIGINT,
	total_tokens BIGINT,
	http_status INTEGER,
	provider_request_id TEXT,
	error_code TEXT,
	start_time TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_platform_model_call_task_project FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT fk_ai_platform_model_call_node FOREIGN KEY(task_id, task_attempt_id, node_id) REFERENCES ai_workflow_node (task_id, task_attempt_id, id),
	CONSTRAINT fk_ai_platform_model_call_provider FOREIGN KEY(project_key, provider_key) REFERENCES ai_platform_model_provider (project_key, key),
	CONSTRAINT fk_ai_platform_model_call_policy_project FOREIGN KEY(policy_capability_id, project_key) REFERENCES ai_capability (id, project_key),
	CONSTRAINT fk_ai_platform_model_call_policy_version FOREIGN KEY(policy_capability_id, policy_version) REFERENCES ai_capability_version (capability_id, version),
	CONSTRAINT uk_ai_platform_model_call_node_model UNIQUE (node_id, requested_model),
	CONSTRAINT fk_ai_platform_model_call_agent_step FOREIGN KEY(task_id, task_attempt_id, agent_execution_id, agent_step_id, agent_step_kind) REFERENCES ai_agent_step (task_id, task_attempt_id, agent_execution_id, id, kind),
	CONSTRAINT ck_ai_platform_model_call_owner CHECK ((node_id IS NOT NULL AND agent_execution_id IS NULL AND agent_step_id IS NULL AND agent_step_kind IS NULL) OR (node_id IS NULL AND agent_execution_id IS NOT NULL AND agent_step_id IS NOT NULL AND agent_step_kind IS NOT NULL AND agent_step_kind='model_turn')),
	CONSTRAINT uk_ai_platform_model_call_step_model UNIQUE (agent_step_id, requested_model),
	CONSTRAINT ck_ai_platform_model_call_status CHECK (status IN ('running','succeeded','failed','cancelled')),
	CONSTRAINT ck_ai_platform_model_call_terminal CHECK ((status = 'running' AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND actual_model IS NOT NULL AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^MODEL_[A-Z0-9_]{1,90}$')),
	CONSTRAINT ck_ai_platform_model_call_time CHECK (finish_time IS NULL OR finish_time >= start_time),
	CONSTRAINT ck_ai_platform_model_call_hashes CHECK (policy_hash ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$' AND (prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$')),
	CONSTRAINT ck_ai_platform_model_call_prompt CHECK ((prompt_key IS NULL AND prompt_version IS NULL AND prompt_hash IS NULL) OR (prompt_key IS NOT NULL AND prompt_version IS NOT NULL AND prompt_hash IS NOT NULL)),
	CONSTRAINT ck_ai_platform_model_call_usage CHECK ((prompt_tokens IS NULL OR prompt_tokens >= 0) AND (completion_tokens IS NULL OR completion_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)),
	CONSTRAINT ck_ai_platform_model_call_http CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
	CONSTRAINT ck_ai_platform_model_call_request_id CHECK (provider_request_id IS NULL OR length(provider_request_id) <= 512)
);

CREATE TABLE ai_platform_tool_call (
	id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	task_attempt_id TEXT NOT NULL,
	node_id TEXT,
	agent_execution_id TEXT,
	agent_step_id TEXT,
	agent_step_kind TEXT,
	tool_capability_id TEXT NOT NULL,
	tool_key TEXT NOT NULL,
	tool_version TEXT NOT NULL,
	tool_hash TEXT NOT NULL,
	binding_key TEXT NOT NULL,
	handler_key TEXT NOT NULL,
	handler_version TEXT NOT NULL,
	operation_type TEXT NOT NULL,
	status TEXT NOT NULL,
	input_hash TEXT NOT NULL,
	output_hash TEXT,
	error_code TEXT,
	start_time TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT fk_ai_platform_tool_call_task_project FOREIGN KEY(task_id, project_key) REFERENCES ai_task (id, project_key),
	CONSTRAINT fk_ai_platform_tool_call_node FOREIGN KEY(task_id, task_attempt_id, node_id) REFERENCES ai_workflow_node (task_id, task_attempt_id, id),
	CONSTRAINT fk_ai_platform_tool_call_binding FOREIGN KEY(project_key, binding_key) REFERENCES ai_platform_tool_binding (project_key, key),
	CONSTRAINT fk_ai_platform_tool_call_capability_project FOREIGN KEY(tool_capability_id, project_key) REFERENCES ai_capability (id, project_key),
	CONSTRAINT fk_ai_platform_tool_call_version FOREIGN KEY(tool_capability_id, tool_version) REFERENCES ai_capability_version (capability_id, version),
	CONSTRAINT uk_ai_platform_tool_call_node UNIQUE (node_id),
	CONSTRAINT fk_ai_platform_tool_call_agent_step FOREIGN KEY(task_id, task_attempt_id, agent_execution_id, agent_step_id, agent_step_kind) REFERENCES ai_agent_step (task_id, task_attempt_id, agent_execution_id, id, kind),
	CONSTRAINT ck_ai_platform_tool_call_owner CHECK ((node_id IS NOT NULL AND agent_execution_id IS NULL AND agent_step_id IS NULL AND agent_step_kind IS NULL) OR (node_id IS NULL AND agent_execution_id IS NOT NULL AND agent_step_id IS NOT NULL AND agent_step_kind IS NOT NULL AND agent_step_kind='tool_call')),
	CONSTRAINT uk_ai_platform_tool_call_step UNIQUE (agent_step_id),
	CONSTRAINT ck_ai_platform_tool_call_operation CHECK (operation_type = 'read_only'),
	CONSTRAINT ck_ai_platform_tool_call_status CHECK (status IN ('running','succeeded','failed','cancelled')),
	CONSTRAINT ck_ai_platform_tool_call_terminal CHECK ((status = 'running' AND finish_time IS NULL AND output_hash IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND finish_time IS NOT NULL AND output_hash IS NOT NULL AND output_hash ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR (status IN ('failed','cancelled') AND finish_time IS NOT NULL AND output_hash IS NULL AND error_code IS NOT NULL AND error_code ~ '^TOOL_[A-Z0-9_]{1,90}$')),
	CONSTRAINT ck_ai_platform_tool_call_time CHECK (finish_time IS NULL OR finish_time >= start_time),
	CONSTRAINT ck_ai_platform_tool_call_hashes CHECK (tool_hash ~ '^[0-9a-f]{64}$' AND input_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE ai_budget_charge (
	call_id TEXT NOT NULL,
	project_key TEXT NOT NULL,
	budget_key TEXT NOT NULL,
	task_id TEXT NOT NULL,
	reserved_microunits BIGINT NOT NULL,
	settled_microunits BIGINT,
	state TEXT NOT NULL,
	reconciliation_ref TEXT,
	PRIMARY KEY (call_id),
	FOREIGN KEY(project_key, budget_key) REFERENCES ai_budget_cycle (project_key, key),
	CONSTRAINT ck_ai_budget_charge CHECK (reserved_microunits>0 AND (settled_microunits IS NULL OR settled_microunits>=0) AND ((state IN ('reserved','unknown') AND settled_microunits IS NULL) OR (state IN ('known','reconciled') AND settled_microunits IS NOT NULL))),
	FOREIGN KEY(call_id) REFERENCES ai_platform_model_call (id),
	FOREIGN KEY(task_id) REFERENCES ai_task (id)
);

CREATE INDEX idx_ai_run_log_trace ON ai_run_log (trace_id);

CREATE INDEX ix_ai_worker_health_live ON ai_worker_health (state, heartbeat_at);

CREATE INDEX idx_ai_capability_version_status ON ai_capability_version (capability_id, status);

CREATE INDEX ix_ai_quality_run_owner ON ai_quality_run (project_key, owner, id);

CREATE INDEX idx_ai_task_owner_created ON ai_task (project_key, submitted_by, create_time, id);

CREATE INDEX idx_ai_task_queued ON ai_task (create_time, id) WHERE status = 'queued';

CREATE INDEX ix_ai_task_business ON ai_task (project_key, business_id, create_time, id) WHERE business_id IS NOT NULL;

CREATE INDEX ix_ai_task_customer ON ai_task (project_key, customer_id, create_time, id) WHERE customer_id IS NOT NULL;

CREATE INDEX ix_ai_task_project_status ON ai_task (project_key, status, create_time, id);

CREATE INDEX ix_ai_task_running_capability ON ai_task (capability_id) WHERE status = 'running';

CREATE INDEX ix_ai_gateway_request_project_task_created_id ON ai_gateway_request (project_key, task_id, create_time, id);

CREATE INDEX ix_ai_memory_fact_current ON ai_memory_fact (project_key, owner, capability_key, subject_key, fact_key, revision);

CREATE INDEX idx_ai_task_attempt_expiry ON ai_task_attempt (lease_expires_at) WHERE status = 'running';

CREATE INDEX idx_ai_task_attempt_task ON ai_task_attempt (task_id, number);

CREATE UNIQUE INDEX uk_ai_task_attempt_running ON ai_task_attempt (task_id) WHERE status = 'running';

CREATE INDEX ix_ai_task_feedback_task ON ai_task_feedback (project_key, task_id, submitter, revision);

CREATE INDEX ix_ai_task_review_task ON ai_task_review (project_key, task_id, id);

CREATE INDEX idx_ai_workflow_node_running ON ai_workflow_node (task_attempt_id) WHERE status = 'running';

CREATE INDEX idx_ai_workflow_node_task ON ai_workflow_node (task_id, task_attempt_id, start_time, node_key);

CREATE INDEX ix_ai_agent_execution_running_capability ON ai_agent_execution (agent_capability_id) WHERE parent_node_id IS NOT NULL AND status = 'running';

CREATE UNIQUE INDEX uk_ai_agent_execution_root ON ai_agent_execution (task_attempt_id) WHERE parent_node_id IS NULL;

CREATE INDEX ix_ai_callback_delivery_due ON ai_callback_delivery (state, next_attempt_at, id);

CREATE INDEX ix_ai_callback_delivery_expiry ON ai_callback_delivery (state, lease_expires_at);

CREATE INDEX ix_ai_callback_delivery_project ON ai_callback_delivery (project_key, id);

CREATE INDEX idx_ai_agent_step_execution ON ai_agent_step (agent_execution_id, turn, kind, slot);

CREATE INDEX idx_ai_platform_model_call_node ON ai_platform_model_call (task_id, task_attempt_id, node_id, start_time);

CREATE INDEX idx_ai_platform_model_call_owner ON ai_platform_model_call (project_key, task_id, start_time);

CREATE UNIQUE INDEX uk_ai_platform_model_call_running ON ai_platform_model_call (node_id) WHERE status = 'running';

CREATE UNIQUE INDEX uk_ai_platform_model_call_step_running ON ai_platform_model_call (agent_step_id) WHERE status = 'running';

CREATE INDEX idx_ai_platform_tool_call_owner ON ai_platform_tool_call (project_key, task_id, start_time);

CREATE INDEX ix_ai_budget_charge_cycle ON ai_budget_charge (project_key, budget_key, call_id);

CREATE INDEX ix_ai_budget_charge_task ON ai_budget_charge (task_id, state);

INSERT INTO ai_project (key, name, description, enabled, created_by, create_time)
VALUES ('platform', 'Platform', '', TRUE, 'bootstrap', CURRENT_TIMESTAMP);
