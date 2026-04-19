BEGIN;

CREATE TABLE alembic_version (
    version_num TEXT NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001_baseline_auth_and_close_runs

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    email CITEXT NOT NULL, 
    password_hash TEXT NOT NULL, 
    full_name TEXT NOT NULL, 
    status TEXT DEFAULT 'active' NOT NULL, 
    last_login_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_users PRIMARY KEY (id), 
    CONSTRAINT ck_users_ck_users_status_valid CHECK (status IN ('active', 'disabled')), 
    CONSTRAINT uq_users_email UNIQUE (email)
);

CREATE TABLE entities (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    name TEXT NOT NULL, 
    legal_name TEXT, 
    base_currency CHAR(3) DEFAULT 'NGN' NOT NULL, 
    country_code CHAR(2) DEFAULT 'NG' NOT NULL, 
    timezone TEXT DEFAULT 'Africa/Lagos' NOT NULL, 
    accounting_standard TEXT, 
    autonomy_mode TEXT DEFAULT 'human_review' NOT NULL, 
    default_confidence_thresholds JSONB DEFAULT jsonb_build_object('classification', 0.85, 'coding', 0.85, 'reconciliation', 0.9, 'posting', 0.95) NOT NULL, 
    status TEXT DEFAULT 'active' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_entities PRIMARY KEY (id), 
    CONSTRAINT ck_entities_ck_entities_autonomy_mode_valid CHECK (autonomy_mode IN ('human_review', 'reduced_interruption')), 
    CONSTRAINT ck_entities_ck_entities_status_valid CHECK (status IN ('active', 'archived'))
);

CREATE TABLE sessions (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    user_id UUID NOT NULL, 
    session_token_hash TEXT NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    user_agent TEXT, 
    ip_address INET, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_sessions PRIMARY KEY (id), 
    CONSTRAINT uq_sessions_session_token_hash UNIQUE (session_token_hash), 
    CONSTRAINT fk_sessions_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE api_tokens (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    user_id UUID NOT NULL, 
    name TEXT NOT NULL, 
    token_hash TEXT NOT NULL, 
    scope JSONB DEFAULT '[]'::jsonb NOT NULL, 
    last_used_at TIMESTAMP WITH TIME ZONE, 
    revoked_at TIMESTAMP WITH TIME ZONE, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_api_tokens PRIMARY KEY (id), 
    CONSTRAINT uq_api_tokens_token_hash UNIQUE (token_hash), 
    CONSTRAINT fk_api_tokens_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE entity_memberships (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    user_id UUID NOT NULL, 
    role TEXT NOT NULL, 
    is_default_actor BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_entity_memberships PRIMARY KEY (id), 
    CONSTRAINT uq_entity_memberships_entity_user UNIQUE (entity_id, user_id), 
    CONSTRAINT fk_entity_memberships_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_entity_memberships_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE close_runs (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    period_start DATE NOT NULL, 
    period_end DATE NOT NULL, 
    status TEXT NOT NULL, 
    reporting_currency CHAR(3) NOT NULL, 
    current_version_no INTEGER DEFAULT 1 NOT NULL, 
    opened_by_user_id UUID NOT NULL, 
    approved_by_user_id UUID, 
    approved_at TIMESTAMP WITH TIME ZONE, 
    archived_at TIMESTAMP WITH TIME ZONE, 
    reopened_from_close_run_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_close_runs PRIMARY KEY (id), 
    CONSTRAINT ck_close_runs_ck_close_runs_status_valid CHECK (status IN ('draft', 'in_review', 'approved', 'exported', 'archived', 'reopened')), 
    CONSTRAINT ck_close_runs_ck_close_runs_period_range_valid CHECK (period_end >= period_start), 
    CONSTRAINT ck_close_runs_ck_close_runs_current_version_no_positive CHECK (current_version_no >= 1), 
    CONSTRAINT uq_close_runs_entity_period_version UNIQUE (entity_id, period_start, period_end, current_version_no), 
    CONSTRAINT fk_close_runs_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_close_runs_opened_by_user_id_users FOREIGN KEY(opened_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_close_runs_approved_by_user_id_users FOREIGN KEY(approved_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_close_runs_reopened_from_close_run_id_close_runs FOREIGN KEY(reopened_from_close_run_id) REFERENCES close_runs (id)
);

CREATE TABLE close_run_phase_states (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    phase TEXT NOT NULL, 
    status TEXT NOT NULL, 
    blocking_reason TEXT, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_close_run_phase_states PRIMARY KEY (id), 
    CONSTRAINT ck_close_run_phase_states_ck_close_run_phase_states_phase_valid CHECK (phase IN ('collection', 'processing', 'reconciliation', 'reporting', 'review_signoff')), 
    CONSTRAINT ck_close_run_phase_states_ck_close_run_phase_states_sta_1480 CHECK (status IN ('not_started', 'in_progress', 'blocked', 'ready', 'completed')), 
    CONSTRAINT ck_close_run_phase_states_ck_close_run_phase_states_blo_d2fc CHECK ((status = 'blocked' AND blocking_reason IS NOT NULL) OR (status <> 'blocked' AND blocking_reason IS NULL)), 
    CONSTRAINT uq_close_run_phase_states_close_run_phase UNIQUE (close_run_id, phase), 
    CONSTRAINT fk_close_run_phase_states_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id)
);

CREATE TABLE review_actions (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    target_type TEXT NOT NULL, 
    target_id UUID NOT NULL, 
    action TEXT NOT NULL, 
    actor_user_id UUID NOT NULL, 
    autonomy_mode TEXT NOT NULL, 
    reason TEXT, 
    before_payload JSONB, 
    after_payload JSONB, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_review_actions PRIMARY KEY (id), 
    CONSTRAINT ck_review_actions_ck_review_actions_autonomy_mode_valid CHECK (autonomy_mode IN ('human_review', 'reduced_interruption')), 
    CONSTRAINT fk_review_actions_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_review_actions_actor_user_id_users FOREIGN KEY(actor_user_id) REFERENCES users (id)
);

CREATE TABLE audit_events (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    close_run_id UUID, 
    event_type TEXT NOT NULL, 
    actor_user_id UUID, 
    source_surface TEXT NOT NULL, 
    payload JSONB NOT NULL, 
    trace_id TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_audit_events PRIMARY KEY (id), 
    CONSTRAINT ck_audit_events_ck_audit_events_source_surface_valid CHECK (source_surface IN ('desktop', 'cli', 'system', 'worker', 'integration')), 
    CONSTRAINT fk_audit_events_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_audit_events_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_audit_events_actor_user_id_users FOREIGN KEY(actor_user_id) REFERENCES users (id)
);

CREATE INDEX ix_sessions_user_id ON sessions (user_id);

CREATE INDEX ix_sessions_expires_at ON sessions (expires_at);

CREATE INDEX ix_api_tokens_user_id ON api_tokens (user_id);

CREATE INDEX ix_api_tokens_active_user_id ON api_tokens (user_id) WHERE revoked_at IS NULL;

CREATE INDEX ix_entities_name ON entities (name);

CREATE INDEX ix_entities_status ON entities (status);

CREATE INDEX ix_close_runs_entity_id_status ON close_runs (entity_id, status);

CREATE INDEX ix_close_runs_entity_id_period_start_period_end ON close_runs (entity_id, period_start, period_end);

CREATE INDEX ix_review_actions_close_run_id_target_type_target_id ON review_actions (close_run_id, target_type, target_id);

CREATE INDEX ix_audit_events_entity_id_created_at ON audit_events (entity_id, created_at);

CREATE INDEX ix_audit_events_event_type ON audit_events (event_type);

CREATE INDEX ix_audit_events_trace_id ON audit_events (trace_id);

INSERT INTO alembic_version (version_num) VALUES ('0001_baseline_auth_and_close_runs') RETURNING alembic_version.version_num;

-- Running upgrade 0001_baseline_auth_and_close_runs -> 0002_add_integration_connections

CREATE TABLE integration_connections (
    id UUID NOT NULL, 
    entity_id UUID NOT NULL, 
    provider TEXT NOT NULL, 
    status TEXT DEFAULT 'connected' NOT NULL, 
    encrypted_credentials JSONB DEFAULT '{}'::jsonb NOT NULL, 
    external_realm_id TEXT NOT NULL, 
    last_sync_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_integration_connections PRIMARY KEY (id), 
    CONSTRAINT ck_integration_connections_ck_integration_connections_p_fb44 CHECK (provider IN ('quickbooks_online')), 
    CONSTRAINT ck_integration_connections_ck_integration_connections_s_af5c CHECK (status IN ('connected', 'expired', 'revoked', 'error')), 
    CONSTRAINT uq_integration_connections_entity_provider UNIQUE (entity_id, provider), 
    CONSTRAINT fk_integration_connections_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id)
);

CREATE INDEX ix_integration_connections_status ON integration_connections (status);

CREATE INDEX ix_integration_connections_last_sync_at ON integration_connections (last_sync_at);

UPDATE alembic_version SET version_num='0002_add_integration_connections' WHERE alembic_version.version_num = '0001_baseline_auth_and_close_runs';

-- Running upgrade 0002_add_integration_connections -> 0003_ownership_targets

CREATE TABLE ownership_targets (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    close_run_id UUID, 
    target_type TEXT NOT NULL, 
    target_id UUID NOT NULL, 
    owner_user_id UUID, 
    locked_by_user_id UUID, 
    locked_at TIMESTAMP WITH TIME ZONE, 
    last_touched_by_user_id UUID, 
    last_touched_at TIMESTAMP WITH TIME ZONE, 
    lock_note TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_ownership_targets PRIMARY KEY (id), 
    CONSTRAINT ck_ownership_targets_ck_ownership_targets_target_type_valid CHECK (target_type IN ('entity', 'close_run', 'document', 'recommendation', 'review_target')), 
    CONSTRAINT ck_ownership_targets_ck_ownership_targets_lock_metadata_valid CHECK ((locked_by_user_id IS NULL AND locked_at IS NULL) OR (locked_by_user_id IS NOT NULL AND locked_at IS NOT NULL)), 
    CONSTRAINT ck_ownership_targets_ck_ownership_targets_last_touch_me_182c CHECK ((last_touched_by_user_id IS NULL AND last_touched_at IS NULL) OR (last_touched_by_user_id IS NOT NULL AND last_touched_at IS NOT NULL)), 
    CONSTRAINT uq_ownership_targets_type_target UNIQUE (target_type, target_id), 
    CONSTRAINT fk_ownership_targets_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_ownership_targets_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_ownership_targets_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id), 
    CONSTRAINT fk_ownership_targets_locked_by_user_id_users FOREIGN KEY(locked_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_ownership_targets_last_touched_by_user_id_users FOREIGN KEY(last_touched_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_ownership_targets_entity_id ON ownership_targets (entity_id);

CREATE INDEX ix_ownership_targets_close_run_id ON ownership_targets (close_run_id);

CREATE INDEX ix_ownership_targets_locked_by_user_id ON ownership_targets (locked_by_user_id);

UPDATE alembic_version SET version_num='0003_ownership_targets' WHERE alembic_version.version_num = '0002_add_integration_connections';

-- Running upgrade 0003_ownership_targets -> 0004_document_upload_records

CREATE TABLE documents (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    parent_document_id UUID, 
    document_type TEXT DEFAULT 'unknown' NOT NULL, 
    source_channel TEXT DEFAULT 'upload' NOT NULL, 
    storage_key TEXT NOT NULL, 
    original_filename TEXT NOT NULL, 
    mime_type TEXT NOT NULL, 
    file_size_bytes BIGINT NOT NULL, 
    sha256_hash VARCHAR(64) NOT NULL, 
    period_start DATE, 
    period_end DATE, 
    classification_confidence NUMERIC(5, 4), 
    ocr_required BOOLEAN DEFAULT false NOT NULL, 
    status TEXT DEFAULT 'uploaded' NOT NULL, 
    owner_user_id UUID, 
    last_touched_by_user_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_documents PRIMARY KEY (id), 
    CONSTRAINT ck_documents_ck_documents_document_type_valid CHECK (document_type IN ('unknown', 'invoice', 'bank_statement', 'payslip', 'receipt', 'contract')), 
    CONSTRAINT ck_documents_ck_documents_source_channel_valid CHECK (source_channel IN ('upload', 'api_import', 'manual_entry')), 
    CONSTRAINT ck_documents_ck_documents_status_valid CHECK (status IN ('uploaded', 'processing', 'parsed', 'needs_review', 'approved', 'rejected', 'failed', 'duplicate', 'blocked')), 
    CONSTRAINT ck_documents_ck_documents_file_size_bytes_non_negative CHECK (file_size_bytes >= 0), 
    CONSTRAINT ck_documents_ck_documents_sha256_hash_length_valid CHECK (length(sha256_hash) = 64), 
    CONSTRAINT ck_documents_ck_documents_period_range_valid CHECK (period_start IS NULL OR period_end IS NULL OR period_end >= period_start), 
    CONSTRAINT ck_documents_ck_documents_classification_confidence_ratio_valid CHECK (classification_confidence IS NULL OR (classification_confidence >= 0 AND classification_confidence <= 1)), 
    CONSTRAINT fk_documents_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_documents_parent_document_id_documents FOREIGN KEY(parent_document_id) REFERENCES documents (id), 
    CONSTRAINT fk_documents_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id), 
    CONSTRAINT fk_documents_last_touched_by_user_id_users FOREIGN KEY(last_touched_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_documents_close_run_id ON documents (close_run_id);

CREATE INDEX ix_documents_sha256_hash ON documents (sha256_hash);

CREATE INDEX ix_documents_close_run_id_status ON documents (close_run_id, status);

CREATE INDEX ix_documents_original_filename_tsv ON documents USING gin (to_tsvector('simple', original_filename));

CREATE TABLE document_versions (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    document_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    normalized_storage_key TEXT, 
    ocr_text_storage_key TEXT, 
    parser_name TEXT NOT NULL, 
    parser_version TEXT NOT NULL, 
    raw_parse_payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    page_count INTEGER, 
    checksum VARCHAR(64) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_document_versions PRIMARY KEY (id), 
    CONSTRAINT ck_document_versions_ck_document_versions_version_no_positive CHECK (version_no >= 1), 
    CONSTRAINT ck_document_versions_ck_document_versions_page_count_no_fcf6 CHECK (page_count IS NULL OR page_count >= 0), 
    CONSTRAINT ck_document_versions_ck_document_versions_checksum_length_valid CHECK (length(checksum) = 64), 
    CONSTRAINT uq_document_versions_document_version UNIQUE (document_id, version_no), 
    CONSTRAINT fk_document_versions_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id)
);

CREATE INDEX ix_document_versions_document_id ON document_versions (document_id);

CREATE TABLE document_issues (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    document_id UUID NOT NULL, 
    issue_type TEXT NOT NULL, 
    severity TEXT NOT NULL, 
    status TEXT DEFAULT 'open' NOT NULL, 
    details JSONB DEFAULT '{}'::jsonb NOT NULL, 
    assigned_to_user_id UUID, 
    resolved_by_user_id UUID, 
    resolved_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_document_issues PRIMARY KEY (id), 
    CONSTRAINT ck_document_issues_ck_document_issues_severity_valid CHECK (severity IN ('info', 'warning', 'blocking')), 
    CONSTRAINT ck_document_issues_ck_document_issues_status_valid CHECK (status IN ('open', 'resolved', 'dismissed')), 
    CONSTRAINT ck_document_issues_ck_document_issues_resolution_metadata_valid CHECK ((status = 'open' AND resolved_by_user_id IS NULL AND resolved_at IS NULL) OR (status <> 'open' AND resolved_by_user_id IS NOT NULL AND resolved_at IS NOT NULL)), 
    CONSTRAINT fk_document_issues_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id), 
    CONSTRAINT fk_document_issues_assigned_to_user_id_users FOREIGN KEY(assigned_to_user_id) REFERENCES users (id), 
    CONSTRAINT fk_document_issues_resolved_by_user_id_users FOREIGN KEY(resolved_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_document_issues_document_id ON document_issues (document_id);

CREATE INDEX ix_document_issues_status_severity ON document_issues (status, severity);

UPDATE alembic_version SET version_num='0004_document_upload_records' WHERE alembic_version.version_num = '0003_ownership_targets';

-- Running upgrade 0004_document_upload_records -> 0005_document_extractions

CREATE TABLE document_extractions (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    document_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    schema_name VARCHAR(50) NOT NULL, 
    schema_version VARCHAR(20) NOT NULL, 
    extracted_payload JSONB NOT NULL, 
    confidence_summary JSONB NOT NULL, 
    needs_review BOOLEAN DEFAULT false NOT NULL, 
    approved_version BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_document_extractions PRIMARY KEY (id), 
    CONSTRAINT ck_document_extractions_ck_document_extractions_extract_1ffc CHECK (version_no >= 1), 
    CONSTRAINT uq_document_extractions_document_version UNIQUE (document_id, version_no), 
    CONSTRAINT fk_document_extractions_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id)
);

CREATE INDEX ix_document_extractions_document_id ON document_extractions (document_id);

CREATE TABLE extracted_fields (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    document_extraction_id UUID NOT NULL, 
    field_name VARCHAR(100) NOT NULL, 
    field_value JSONB, 
    field_type VARCHAR(20) NOT NULL, 
    confidence NUMERIC(5, 4) NOT NULL, 
    evidence_ref JSONB NOT NULL, 
    is_human_corrected BOOLEAN DEFAULT false NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_extracted_fields PRIMARY KEY (id), 
    CONSTRAINT ck_extracted_fields_ck_extracted_fields_extracted_field_1dbb CHECK (confidence >= 0 AND confidence <= 1), 
    CONSTRAINT fk_extracted_fields_document_extraction_id_document_extractions FOREIGN KEY(document_extraction_id) REFERENCES document_extractions (id)
);

CREATE INDEX ix_extracted_fields_document_extraction_id_field_name ON extracted_fields (document_extraction_id, field_name);

CREATE TABLE document_line_items (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    document_extraction_id UUID NOT NULL, 
    line_no INTEGER NOT NULL, 
    description VARCHAR, 
    quantity NUMERIC(18, 6), 
    unit_price NUMERIC(18, 6), 
    amount NUMERIC(18, 2), 
    tax_amount NUMERIC(18, 2), 
    dimensions JSONB DEFAULT '{}' NOT NULL, 
    evidence_ref JSONB NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_document_line_items PRIMARY KEY (id), 
    CONSTRAINT ck_document_line_items_ck_document_line_items_line_item_33be CHECK (line_no >= 1), 
    CONSTRAINT uq_document_line_items_extraction_line UNIQUE (document_extraction_id, line_no), 
    CONSTRAINT fk_document_line_items_document_extraction_id_document__21e7 FOREIGN KEY(document_extraction_id) REFERENCES document_extractions (id)
);

CREATE INDEX ix_document_line_items_document_extraction_id ON document_line_items (document_extraction_id);

UPDATE alembic_version SET version_num='0005_document_extractions' WHERE alembic_version.version_num = '0004_document_upload_records';

-- Running upgrade 0005_document_extractions -> 0006_chart_of_accounts

CREATE TABLE coa_sets (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    source TEXT NOT NULL, 
    version_no INTEGER NOT NULL, 
    is_active BOOLEAN DEFAULT false NOT NULL, 
    import_metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
    activated_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_coa_sets PRIMARY KEY (id), 
    CONSTRAINT ck_coa_sets_ck_coa_sets_source_valid CHECK (source IN ('manual_upload', 'quickbooks_sync', 'fallback_nigerian_sme')), 
    CONSTRAINT ck_coa_sets_ck_coa_sets_version_no_positive CHECK (version_no >= 1), 
    CONSTRAINT uq_coa_sets_entity_version UNIQUE (entity_id, version_no), 
    CONSTRAINT fk_coa_sets_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id)
);

CREATE INDEX ix_coa_sets_entity_id_source ON coa_sets (entity_id, source);

CREATE INDEX ix_coa_sets_entity_id_version_no ON coa_sets (entity_id, version_no);

CREATE UNIQUE INDEX uq_coa_sets_entity_active ON coa_sets (entity_id) WHERE is_active;

CREATE TABLE coa_accounts (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    coa_set_id UUID NOT NULL, 
    account_code TEXT NOT NULL, 
    account_name TEXT NOT NULL, 
    account_type TEXT NOT NULL, 
    parent_account_id UUID, 
    is_postable BOOLEAN DEFAULT true NOT NULL, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    external_ref TEXT, 
    dimension_defaults JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_coa_accounts PRIMARY KEY (id), 
    CONSTRAINT uq_coa_accounts_set_code UNIQUE (coa_set_id, account_code), 
    CONSTRAINT fk_coa_accounts_coa_set_id_coa_sets FOREIGN KEY(coa_set_id) REFERENCES coa_sets (id), 
    CONSTRAINT fk_coa_accounts_parent_account_id_coa_accounts FOREIGN KEY(parent_account_id) REFERENCES coa_accounts (id)
);

CREATE INDEX ix_coa_accounts_coa_set_id_account_type ON coa_accounts (coa_set_id, account_type);

CREATE INDEX ix_coa_accounts_coa_set_id_account_code ON coa_accounts (coa_set_id, account_code);

CREATE TABLE coa_mapping_rules (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    name TEXT NOT NULL, 
    priority INTEGER DEFAULT 100 NOT NULL, 
    match_conditions JSONB DEFAULT '{}'::jsonb NOT NULL, 
    target_account_id UUID NOT NULL, 
    target_dimensions JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_from_override BOOLEAN DEFAULT false NOT NULL, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_coa_mapping_rules PRIMARY KEY (id), 
    CONSTRAINT ck_coa_mapping_rules_ck_coa_mapping_rules_priority_non_negative CHECK (priority >= 0), 
    CONSTRAINT fk_coa_mapping_rules_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_coa_mapping_rules_target_account_id_coa_accounts FOREIGN KEY(target_account_id) REFERENCES coa_accounts (id)
);

CREATE INDEX ix_coa_mapping_rules_entity_priority ON coa_mapping_rules (entity_id, priority);

CREATE INDEX ix_coa_mapping_rules_entity_active ON coa_mapping_rules (entity_id, is_active);

UPDATE alembic_version SET version_num='0006_chart_of_accounts' WHERE alembic_version.version_num = '0005_document_extractions';

-- Running upgrade 0006_chart_of_accounts -> 0007_recommendations

CREATE TABLE recommendations (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    document_id UUID, 
    recommendation_type VARCHAR(120) NOT NULL, 
    status VARCHAR(30) DEFAULT 'draft' NOT NULL, 
    payload JSONB DEFAULT '{}' NOT NULL, 
    confidence NUMERIC(5, 4) NOT NULL, 
    reasoning_summary VARCHAR(5000) NOT NULL, 
    evidence_links JSONB DEFAULT '[]' NOT NULL, 
    prompt_version VARCHAR(30) NOT NULL, 
    rule_version VARCHAR(30) NOT NULL, 
    schema_version VARCHAR(30) NOT NULL, 
    created_by_system BOOLEAN DEFAULT true NOT NULL, 
    superseded_by_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_recommendations PRIMARY KEY (id), 
    CONSTRAINT fk_recommendations_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_recommendations_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE SET NULL, 
    CONSTRAINT fk_recommendations_superseded_by_id_recommendations FOREIGN KEY(superseded_by_id) REFERENCES recommendations (id) ON DELETE SET NULL, 
    CONSTRAINT ck_recommendations_recommendations_confidence_range CHECK (confidence >= 0 AND confidence <= 1)
);

COMMENT ON COLUMN recommendations.recommendation_type IS 'Canonical recommendation type (e.g., ''gl_coding'', ''journal_draft'').';

COMMENT ON COLUMN recommendations.status IS 'Review lifecycle state of the recommendation.';

COMMENT ON COLUMN recommendations.payload IS 'Structured recommendation payload (accounts, reasoning, risk).';

COMMENT ON COLUMN recommendations.confidence IS 'Aggregate confidence score between 0 and 1.';

COMMENT ON COLUMN recommendations.reasoning_summary IS 'Human-readable reasoning narrative for reviewer consumption.';

COMMENT ON COLUMN recommendations.evidence_links IS 'Structured references to supporting evidence sources.';

COMMENT ON COLUMN recommendations.prompt_version IS 'Version of the prompt template used.';

COMMENT ON COLUMN recommendations.rule_version IS 'Version of the deterministic rules used.';

COMMENT ON COLUMN recommendations.schema_version IS 'Version of the output schema this recommendation conforms to.';

COMMENT ON COLUMN recommendations.created_by_system IS 'Whether the recommendation was system-generated or manually created.';

COMMENT ON COLUMN recommendations.superseded_by_id IS 'ID of the recommendation that superseded this one.';

CREATE INDEX ix_recommendations_close_run_status ON recommendations (close_run_id, status);

CREATE INDEX ix_recommendations_document_type ON recommendations (document_id, recommendation_type);

UPDATE alembic_version SET version_num='0007_recommendations' WHERE alembic_version.version_num = '0006_chart_of_accounts';

-- Running upgrade 0007_recommendations -> 0008_journals

ALTER TABLE recommendations ADD COLUMN autonomy_mode VARCHAR(30);

COMMENT ON COLUMN recommendations.autonomy_mode IS 'Autonomy mode active when the recommendation was created.';

CREATE TABLE journal_entries (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    close_run_id UUID NOT NULL, 
    recommendation_id UUID, 
    journal_number VARCHAR(60) NOT NULL, 
    posting_date DATE NOT NULL, 
    status VARCHAR(30) DEFAULT 'draft' NOT NULL, 
    description TEXT NOT NULL, 
    total_debits NUMERIC(20, 2) NOT NULL, 
    total_credits NUMERIC(20, 2) NOT NULL, 
    line_count INTEGER NOT NULL, 
    source_surface VARCHAR(30) DEFAULT 'system' NOT NULL, 
    autonomy_mode VARCHAR(30), 
    reasoning_summary TEXT, 
    metadata_payload JSONB DEFAULT '{}' NOT NULL, 
    approved_by_user_id UUID, 
    applied_by_user_id UUID, 
    superseded_by_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_journal_entries PRIMARY KEY (id), 
    CONSTRAINT fk_journal_entries_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE, 
    CONSTRAINT fk_journal_entries_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_journal_entries_recommendation_id_recommendations FOREIGN KEY(recommendation_id) REFERENCES recommendations (id) ON DELETE SET NULL, 
    CONSTRAINT fk_journal_entries_approved_by_user_id_users FOREIGN KEY(approved_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_journal_entries_applied_by_user_id_users FOREIGN KEY(applied_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_journal_entries_superseded_by_id_journal_entries FOREIGN KEY(superseded_by_id) REFERENCES journal_entries (id) ON DELETE SET NULL, 
    CONSTRAINT ck_journal_entries_journal_debits_equal_credits CHECK (total_debits = total_credits), 
    CONSTRAINT ck_journal_entries_journal_minimum_lines CHECK (line_count >= 2), 
    CONSTRAINT uq_journal_entries_journal_number UNIQUE (journal_number)
);

COMMENT ON COLUMN journal_entries.journal_number IS 'Human-readable journal identifier (e.g., ''JE-2026-00001'').';

COMMENT ON COLUMN journal_entries.posting_date IS 'Accounting date for the journal posting.';

COMMENT ON COLUMN journal_entries.status IS 'Review lifecycle state of the journal entry.';

COMMENT ON COLUMN journal_entries.description IS 'Narrative description of the journal entry purpose.';

COMMENT ON COLUMN journal_entries.total_debits IS 'Sum of all debit line amounts. Must equal total_credits.';

COMMENT ON COLUMN journal_entries.total_credits IS 'Sum of all credit line amounts. Must equal total_debits.';

COMMENT ON COLUMN journal_entries.line_count IS 'Number of journal lines attached to this entry.';

COMMENT ON COLUMN journal_entries.source_surface IS 'Surface that created the journal (system, desktop, cli, chat).';

COMMENT ON COLUMN journal_entries.autonomy_mode IS 'Autonomy mode active when the journal was created.';

COMMENT ON COLUMN journal_entries.reasoning_summary IS 'Explanation of why this journal was generated.';

COMMENT ON COLUMN journal_entries.metadata_payload IS 'Additional structured metadata (rule version, prompt version, etc.).';

CREATE INDEX ix_journal_entries_close_run_status ON journal_entries (close_run_id, status);

CREATE INDEX ix_journal_entries_recommendation ON journal_entries (recommendation_id);

CREATE INDEX ix_journal_entries_entity_period ON journal_entries (entity_id, posting_date);

CREATE TABLE journal_lines (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    journal_entry_id UUID NOT NULL, 
    line_no INTEGER NOT NULL, 
    account_code VARCHAR(60) NOT NULL, 
    line_type VARCHAR(10) NOT NULL, 
    amount NUMERIC(20, 2) NOT NULL, 
    description TEXT, 
    dimensions JSONB DEFAULT '{}' NOT NULL, 
    reference VARCHAR(120), 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_journal_lines PRIMARY KEY (id), 
    CONSTRAINT fk_journal_lines_journal_entry_id FOREIGN KEY(journal_entry_id) REFERENCES journal_entries (id) ON DELETE CASCADE, 
    CONSTRAINT ck_journal_lines_journal_line_amount_positive CHECK (amount > 0), 
    CONSTRAINT ck_journal_lines_journal_line_no_positive CHECK (line_no >= 1)
);

COMMENT ON COLUMN journal_lines.line_no IS 'Sequential line number within the journal entry (1-based).';

COMMENT ON COLUMN journal_lines.account_code IS 'GL account code from the active chart of accounts.';

COMMENT ON COLUMN journal_lines.line_type IS 'Either ''debit'' or ''credit''.';

COMMENT ON COLUMN journal_lines.amount IS 'Monetary amount for this line (always positive).';

COMMENT ON COLUMN journal_lines.description IS 'Optional memo or description for this specific line.';

COMMENT ON COLUMN journal_lines.dimensions IS 'Assigned dimensions (cost_centre, department, project).';

COMMENT ON COLUMN journal_lines.reference IS 'Optional external reference or transaction ID.';

CREATE INDEX ix_journal_lines_journal_entry ON journal_lines (journal_entry_id);

CREATE INDEX ix_journal_lines_account_code ON journal_lines (account_code);

UPDATE alembic_version SET version_num='0008_journals' WHERE alembic_version.version_num = '0007_recommendations';

-- Running upgrade 0008_journals -> 0009_reconciliation

CREATE TABLE reconciliations (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    reconciliation_type VARCHAR(40) NOT NULL, 
    status VARCHAR(20) DEFAULT 'draft' NOT NULL, 
    summary JSONB DEFAULT '{}' NOT NULL, 
    blocking_reason TEXT, 
    approved_by_user_id UUID, 
    created_by_user_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_reconciliations PRIMARY KEY (id), 
    CONSTRAINT fk_reconciliations_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_reconciliations_approved_by_user_id_users FOREIGN KEY(approved_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_reconciliations_created_by_user_id_users FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

COMMENT ON COLUMN reconciliations.reconciliation_type IS 'The reconciliation category (bank_reconciliation, ar_ageing, etc.).';

COMMENT ON COLUMN reconciliations.status IS 'Lifecycle state of the reconciliation run.';

COMMENT ON COLUMN reconciliations.summary IS 'Aggregated reconciliation summary (matched count, exceptions, totals).';

COMMENT ON COLUMN reconciliations.blocking_reason IS 'Reason the reconciliation is blocked, required when status is ''blocked''.';

CREATE INDEX ix_reconciliations_close_run_type ON reconciliations (close_run_id, reconciliation_type);

CREATE INDEX ix_reconciliations_close_run_status ON reconciliations (close_run_id, status);

CREATE TABLE reconciliation_items (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    reconciliation_id UUID NOT NULL, 
    source_type VARCHAR(30) NOT NULL, 
    source_ref VARCHAR(200) NOT NULL, 
    match_status VARCHAR(20) DEFAULT 'unmatched' NOT NULL, 
    amount NUMERIC(20, 2) NOT NULL, 
    matched_to JSONB DEFAULT '[]' NOT NULL, 
    difference_amount NUMERIC(20, 2) DEFAULT '0.00' NOT NULL, 
    explanation TEXT, 
    requires_disposition BOOLEAN DEFAULT 'false' NOT NULL, 
    disposition VARCHAR(20), 
    disposition_reason TEXT, 
    disposition_by_user_id UUID, 
    dimensions JSONB DEFAULT '{}' NOT NULL, 
    period_date VARCHAR(10), 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_reconciliation_items PRIMARY KEY (id), 
    CONSTRAINT fk_reconciliation_items_reconciliation_id_reconciliations FOREIGN KEY(reconciliation_id) REFERENCES reconciliations (id) ON DELETE CASCADE, 
    CONSTRAINT fk_reconciliation_items_disposition_by_user_id_users FOREIGN KEY(disposition_by_user_id) REFERENCES users (id)
);

COMMENT ON COLUMN reconciliation_items.source_type IS 'What kind of source produced this item.';

COMMENT ON COLUMN reconciliation_items.source_ref IS 'Reference to the originating record.';

COMMENT ON COLUMN reconciliation_items.match_status IS 'Outcome of the matching process for this item.';

COMMENT ON COLUMN reconciliation_items.amount IS 'Monetary amount of this reconciliation item.';

COMMENT ON COLUMN reconciliation_items.matched_to IS 'List of counterpart references this item was matched to.';

COMMENT ON COLUMN reconciliation_items.difference_amount IS 'Difference between this item and its matched counterpart(s).';

COMMENT ON COLUMN reconciliation_items.explanation IS 'System-generated or reviewer-provided explanation of the match outcome.';

COMMENT ON COLUMN reconciliation_items.requires_disposition IS 'Whether a reviewer must disposition this item before sign-off.';

COMMENT ON COLUMN reconciliation_items.disposition IS 'Reviewer disposition choice when the item was resolved.';

COMMENT ON COLUMN reconciliation_items.disposition_reason IS 'Reviewer-provided reasoning for the disposition.';

COMMENT ON COLUMN reconciliation_items.dimensions IS 'Accounting dimensions (cost_centre, department, project) if applicable.';

COMMENT ON COLUMN reconciliation_items.period_date IS 'Accounting period date associated with this item (YYYY-MM-DD).';

CREATE INDEX ix_reconciliation_items_reconciliation ON reconciliation_items (reconciliation_id);

CREATE INDEX ix_reconciliation_items_match_status ON reconciliation_items (reconciliation_id, match_status);

CREATE INDEX ix_reconciliation_items_source ON reconciliation_items (source_type, source_ref);

CREATE TABLE trial_balance_snapshots (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    snapshot_no INTEGER NOT NULL, 
    total_debits NUMERIC(20, 2) NOT NULL, 
    total_credits NUMERIC(20, 2) NOT NULL, 
    is_balanced BOOLEAN NOT NULL, 
    account_balances JSONB DEFAULT '[]' NOT NULL, 
    generated_by_user_id UUID, 
    metadata_payload JSONB DEFAULT '{}' NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_trial_balance_snapshots PRIMARY KEY (id), 
    CONSTRAINT fk_trial_balance_snapshots_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_trial_balance_snapshots_generated_by_user_id_users FOREIGN KEY(generated_by_user_id) REFERENCES users (id)
);

COMMENT ON COLUMN trial_balance_snapshots.snapshot_no IS 'Sequential snapshot number within the close run.';

COMMENT ON COLUMN trial_balance_snapshots.total_debits IS 'Sum of all debit balances in this snapshot.';

COMMENT ON COLUMN trial_balance_snapshots.total_credits IS 'Sum of all credit balances in this snapshot.';

COMMENT ON COLUMN trial_balance_snapshots.is_balanced IS 'Whether total debits equal total credits within tolerance.';

COMMENT ON COLUMN trial_balance_snapshots.account_balances IS 'List of per-account balance records (code, name, debit, credit, net).';

COMMENT ON COLUMN trial_balance_snapshots.metadata_payload IS 'Additional context (rule version, coa set version, generation timestamp).';

CREATE INDEX ix_trial_balance_snapshots_close_run ON trial_balance_snapshots (close_run_id);

CREATE UNIQUE INDEX ix_trial_balance_snapshots_close_run_no ON trial_balance_snapshots (close_run_id, snapshot_no);

CREATE TABLE reconciliation_anomalies (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    trial_balance_snapshot_id UUID, 
    anomaly_type VARCHAR(30) NOT NULL, 
    severity VARCHAR(10) NOT NULL, 
    account_code VARCHAR(60), 
    description TEXT NOT NULL, 
    details JSONB DEFAULT '{}' NOT NULL, 
    resolved BOOLEAN DEFAULT 'false' NOT NULL, 
    resolved_by_user_id UUID, 
    resolution_note TEXT, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_reconciliation_anomalies PRIMARY KEY (id), 
    CONSTRAINT fk_reconciliation_anomalies_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_reconciliation_anomalies_trial_balance_snapshot_id_t_ecfd FOREIGN KEY(trial_balance_snapshot_id) REFERENCES trial_balance_snapshots (id) ON DELETE SET NULL, 
    CONSTRAINT fk_reconciliation_anomalies_resolved_by_user_id_users FOREIGN KEY(resolved_by_user_id) REFERENCES users (id)
);

COMMENT ON COLUMN reconciliation_anomalies.trial_balance_snapshot_id IS 'The trial balance snapshot this anomaly was detected against, if applicable.';

COMMENT ON COLUMN reconciliation_anomalies.anomaly_type IS 'Category of the anomaly (imbalance, unusual balance, variance, etc.).';

COMMENT ON COLUMN reconciliation_anomalies.severity IS 'Severity level: info, warning, or blocking.';

COMMENT ON COLUMN reconciliation_anomalies.account_code IS 'GL account code associated with the anomaly, if applicable.';

COMMENT ON COLUMN reconciliation_anomalies.description IS 'Human-readable description of the anomaly for reviewer investigation.';

COMMENT ON COLUMN reconciliation_anomalies.details IS 'Structured details (expected value, actual value, variance, threshold).';

COMMENT ON COLUMN reconciliation_anomalies.resolved IS 'Whether a reviewer has investigated and resolved this anomaly.';

COMMENT ON COLUMN reconciliation_anomalies.resolution_note IS 'Reviewer-provided reasoning for resolving the anomaly.';

CREATE INDEX ix_reconciliation_anomalies_close_run ON reconciliation_anomalies (close_run_id);

CREATE INDEX ix_reconciliation_anomalies_close_run_severity ON reconciliation_anomalies (close_run_id, severity);

CREATE INDEX ix_reconciliation_anomalies_type ON reconciliation_anomalies (anomaly_type);

UPDATE alembic_version SET version_num='0009_reconciliation' WHERE alembic_version.version_num = '0008_journals';

-- Running upgrade 0009_reconciliation -> 0010_report_templates_and_runs

CREATE TABLE report_templates (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID, 
    source TEXT NOT NULL, 
    version_no INTEGER NOT NULL, 
    name TEXT NOT NULL, 
    description TEXT, 
    is_active BOOLEAN DEFAULT false NOT NULL, 
    sections JSONB DEFAULT '[]'::jsonb NOT NULL, 
    guardrail_config JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_by_user_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_report_templates PRIMARY KEY (id), 
    CONSTRAINT ck_report_templates_ck_report_templates_source_valid CHECK (source IN ('global_default', 'entity_custom')), 
    CONSTRAINT ck_report_templates_ck_report_templates_version_no_positive CHECK (version_no >= 1), 
    CONSTRAINT ck_report_templates_ck_report_templates_sections_must_be_array CHECK (jsonb_typeof(sections) = 'array'), 
    CONSTRAINT uq_report_templates_entity_version UNIQUE (entity_id, version_no), 
    CONSTRAINT fk_report_templates_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_report_templates_created_by_user_id_users FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX uq_report_templates_entity_active ON report_templates (entity_id) WHERE is_active AND entity_id IS NOT NULL;

CREATE INDEX ix_report_templates_entity_id ON report_templates (entity_id);

CREATE INDEX ix_report_templates_is_active ON report_templates (is_active);

CREATE INDEX ix_report_templates_source ON report_templates (source);

COMMENT ON COLUMN report_templates.entity_id IS 'Owning entity workspace for entity-scoped templates, or NULL for global defaults.';

COMMENT ON COLUMN report_templates.source IS 'Template provenance: global_default or entity_custom.';

COMMENT ON COLUMN report_templates.version_no IS 'Monotonic version number for the template lineage.';

COMMENT ON COLUMN report_templates.name IS 'Human-readable template name exposed in the UI.';

COMMENT ON COLUMN report_templates.description IS 'Optional template description for operator context.';

COMMENT ON COLUMN report_templates.is_active IS 'Whether this template is the active one for its entity or global scope.';

COMMENT ON COLUMN report_templates.sections IS 'Ordered array of section definitions.  Each entry includes a ''key'' matching ReportSectionKey, a ''label'', a ''display_order'', and optional ''config''.';

COMMENT ON COLUMN report_templates.guardrail_config IS 'Guardrail metadata: required_section_keys, allow_custom_sections, and any template-level policy overrides.';

COMMENT ON COLUMN report_templates.created_by_user_id IS 'User who created this template version, if attributable.';

CREATE TABLE report_template_sections (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    template_id UUID NOT NULL, 
    section_key TEXT NOT NULL, 
    label TEXT NOT NULL, 
    display_order INTEGER NOT NULL, 
    is_required BOOLEAN DEFAULT true NOT NULL, 
    section_config JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_report_template_sections PRIMARY KEY (id), 
    CONSTRAINT uq_report_template_sections_template_key UNIQUE (template_id, section_key), 
    CONSTRAINT fk_report_template_sections_template_id_report_templates FOREIGN KEY(template_id) REFERENCES report_templates (id)
);

CREATE INDEX ix_report_template_sections_template_id ON report_template_sections (template_id);

CREATE INDEX ix_report_template_sections_section_key ON report_template_sections (section_key);

COMMENT ON COLUMN report_template_sections.template_id IS 'Parent report template this section belongs to.';

COMMENT ON COLUMN report_template_sections.section_key IS 'Stable section identifier (canonical or custom).';

COMMENT ON COLUMN report_template_sections.label IS 'Human-readable section label for UI rendering.';

COMMENT ON COLUMN report_template_sections.display_order IS 'Zero-based rendering order within the template.';

COMMENT ON COLUMN report_template_sections.is_required IS 'Whether the section is mandatory and protected by guardrails.';

COMMENT ON COLUMN report_template_sections.section_config IS 'Optional per-section configuration (filters, formats, etc.).';

CREATE TABLE report_runs (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    template_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    status TEXT NOT NULL, 
    failure_reason TEXT, 
    generation_config JSONB DEFAULT '{}'::jsonb NOT NULL, 
    artifact_refs JSONB DEFAULT '[]'::jsonb NOT NULL, 
    generated_by_user_id UUID, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_report_runs PRIMARY KEY (id), 
    CONSTRAINT ck_report_runs_ck_report_runs_status_valid CHECK (status IN ('pending', 'generating', 'completed', 'failed', 'canceled')), 
    CONSTRAINT ck_report_runs_ck_report_runs_version_no_positive CHECK (version_no >= 1), 
    CONSTRAINT fk_report_runs_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_report_runs_template_id_report_templates FOREIGN KEY(template_id) REFERENCES report_templates (id), 
    CONSTRAINT fk_report_runs_generated_by_user_id_users FOREIGN KEY(generated_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_report_runs_close_run_id ON report_runs (close_run_id);

CREATE INDEX ix_report_runs_status ON report_runs (status);

CREATE INDEX ix_report_runs_close_run_version ON report_runs (close_run_id, version_no);

CREATE INDEX ix_report_runs_template_id ON report_runs (template_id);

COMMENT ON COLUMN report_runs.close_run_id IS 'Close run this report pack was generated for.';

COMMENT ON COLUMN report_runs.template_id IS 'Report template version used for this generation run.';

COMMENT ON COLUMN report_runs.version_no IS 'Monotonic run number within the close run scope.';

COMMENT ON COLUMN report_runs.status IS 'Current lifecycle state of the report generation run.';

COMMENT ON COLUMN report_runs.failure_reason IS 'Structured failure description when status is ''failed''.';

COMMENT ON COLUMN report_runs.generation_config IS 'Generation parameters: requested_sections, period_overrides, commentary_version, etc.';

COMMENT ON COLUMN report_runs.artifact_refs IS 'Array of storage references for generated artifacts (Excel, PDF, evidence packs).';

COMMENT ON COLUMN report_runs.generated_by_user_id IS 'User who triggered this report generation run.';

COMMENT ON COLUMN report_runs.completed_at IS 'UTC timestamp when the run reached a terminal state.';

CREATE TABLE report_commentary (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    report_run_id UUID NOT NULL, 
    section_key TEXT NOT NULL, 
    status TEXT NOT NULL, 
    body TEXT DEFAULT '' NOT NULL, 
    authored_by_user_id UUID, 
    superseded_by_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_report_commentary PRIMARY KEY (id), 
    CONSTRAINT ck_report_commentary_ck_report_commentary_status_valid CHECK (status IN ('draft', 'under_review', 'approved', 'superseded')), 
    CONSTRAINT fk_report_commentary_report_run_id_report_runs FOREIGN KEY(report_run_id) REFERENCES report_runs (id), 
    CONSTRAINT fk_report_commentary_authored_by_user_id_users FOREIGN KEY(authored_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_report_commentary_superseded_by_id_report_commentary FOREIGN KEY(superseded_by_id) REFERENCES report_commentary (id)
);

CREATE INDEX ix_report_commentary_run_section_active ON report_commentary (report_run_id, section_key) WHERE status IN ('draft', 'under_review', 'approved');

CREATE INDEX ix_report_commentary_section_key ON report_commentary (section_key);

CREATE INDEX ix_report_commentary_report_run_id ON report_commentary (report_run_id);

COMMENT ON COLUMN report_commentary.report_run_id IS 'Parent report run this commentary belongs to.';

COMMENT ON COLUMN report_commentary.section_key IS 'Report section this commentary text applies to.';

COMMENT ON COLUMN report_commentary.status IS 'Current lifecycle state of this commentary version.';

COMMENT ON COLUMN report_commentary.body IS 'Commentary text content managed by reviewers.';

COMMENT ON COLUMN report_commentary.authored_by_user_id IS 'User who last edited or approved this commentary version.';

COMMENT ON COLUMN report_commentary.superseded_by_id IS 'Newer commentary row that replaced this version.';

UPDATE alembic_version SET version_num='0010_report_templates_and_runs' WHERE alembic_version.version_num = '0009_reconciliation';

-- Running upgrade 0010_report_templates_and_runs -> 0011_chat_threads_and_messages

CREATE TABLE chat_threads (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    close_run_id UUID, 
    title VARCHAR(300), 
    context_payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_chat_threads PRIMARY KEY (id), 
    CONSTRAINT fk_chat_threads_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE, 
    CONSTRAINT fk_chat_threads_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE SET NULL
);

CREATE INDEX ix_chat_threads_entity_id ON chat_threads (entity_id);

COMMENT ON COLUMN chat_threads.entity_id IS 'Entity workspace that owns this conversation thread.';

COMMENT ON COLUMN chat_threads.close_run_id IS 'Optional close run scoping the thread to a specific accounting period.';

COMMENT ON COLUMN chat_threads.title IS 'Human-readable thread title, auto-generated or user-edited.';

COMMENT ON COLUMN chat_threads.context_payload IS 'Grounding context snapshot (entity, close run, period, autonomy mode).';

CREATE INDEX ix_chat_threads_entity_close_run ON chat_threads (entity_id, close_run_id);

CREATE TABLE chat_messages (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    thread_id UUID NOT NULL, 
    role VARCHAR(20) NOT NULL, 
    content TEXT NOT NULL, 
    message_type VARCHAR(20) DEFAULT '''analysis''' NOT NULL, 
    linked_action_id UUID, 
    grounding_payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    model_metadata JSONB, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_chat_messages PRIMARY KEY (id), 
    CONSTRAINT ck_chat_messages_ck_chat_messages_role CHECK (role IN ('user', 'assistant', 'system')), 
    CONSTRAINT ck_chat_messages_ck_chat_messages_message_type CHECK (message_type IN ('analysis', 'workflow', 'action', 'warning')), 
    CONSTRAINT fk_chat_messages_thread_id_chat_threads FOREIGN KEY(thread_id) REFERENCES chat_threads (id) ON DELETE CASCADE, 
    CONSTRAINT fk_chat_messages_linked_action_id_recommendations FOREIGN KEY(linked_action_id) REFERENCES recommendations (id) ON DELETE SET NULL
);

CREATE INDEX ix_chat_messages_thread_id ON chat_messages (thread_id);

COMMENT ON COLUMN chat_messages.thread_id IS 'Parent chat thread that this message belongs to.';

COMMENT ON COLUMN chat_messages.role IS 'Message originator: user, assistant, or system.';

COMMENT ON COLUMN chat_messages.content IS 'Message text content (Markdown-supported for assistant messages).';

COMMENT ON COLUMN chat_messages.message_type IS 'Intent classification: analysis, workflow, action, or warning.';

COMMENT ON COLUMN chat_messages.linked_action_id IS 'Optional reference to a recommendation discussed in this message.';

COMMENT ON COLUMN chat_messages.grounding_payload IS 'Evidence snapshot used to generate the assistant response.';

COMMENT ON COLUMN chat_messages.model_metadata IS 'Model name, token usage, latency, and provider metadata.';

CREATE INDEX ix_chat_messages_thread_order ON chat_messages (thread_id, created_at);

UPDATE alembic_version SET version_num='0011_chat_threads_and_messages' WHERE alembic_version.version_num = '0010_report_templates_and_runs';

-- Running upgrade 0011_chat_threads_and_messages -> 0012_chat_action_plans

CREATE TABLE chat_action_plans (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    thread_id UUID NOT NULL, 
    message_id UUID, 
    entity_id UUID NOT NULL, 
    close_run_id UUID, 
    actor_user_id UUID NOT NULL, 
    intent VARCHAR(60) NOT NULL, 
    target_type VARCHAR(120), 
    target_id UUID, 
    payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    confidence NUMERIC(5, 4) NOT NULL, 
    autonomy_mode VARCHAR(30) NOT NULL, 
    status VARCHAR(30) DEFAULT '''pending''' NOT NULL, 
    requires_human_approval BOOLEAN DEFAULT true NOT NULL, 
    reasoning VARCHAR(3000) DEFAULT '''''' NOT NULL, 
    applied_result JSONB, 
    rejected_reason VARCHAR(500), 
    superseded_by_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_chat_action_plans PRIMARY KEY (id), 
    CONSTRAINT ck_chat_action_plans_chat_action_plans_confidence_range CHECK (confidence >= 0 AND confidence <= 1), 
    CONSTRAINT fk_chat_action_plans_thread_id_chat_threads FOREIGN KEY(thread_id) REFERENCES chat_threads (id) ON DELETE CASCADE, 
    CONSTRAINT fk_chat_action_plans_message_id_chat_messages FOREIGN KEY(message_id) REFERENCES chat_messages (id) ON DELETE SET NULL, 
    CONSTRAINT fk_chat_action_plans_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE, 
    CONSTRAINT fk_chat_action_plans_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_chat_action_plans_actor_user_id_users FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_chat_action_plans_superseded_by_id_chat_action_plans FOREIGN KEY(superseded_by_id) REFERENCES chat_action_plans (id) ON DELETE SET NULL
);

CREATE INDEX ix_chat_action_plans_thread_id ON chat_action_plans (thread_id);

CREATE INDEX ix_chat_action_plans_entity_id ON chat_action_plans (entity_id);

COMMENT ON COLUMN chat_action_plans.thread_id IS 'Chat thread where this action originated.';

COMMENT ON COLUMN chat_action_plans.message_id IS 'Message that triggered the action, if attributable.';

COMMENT ON COLUMN chat_action_plans.entity_id IS 'Entity workspace that owns this action plan.';

COMMENT ON COLUMN chat_action_plans.close_run_id IS 'Close run scope if the action is period-specific.';

COMMENT ON COLUMN chat_action_plans.actor_user_id IS 'User whose message triggered the action.';

COMMENT ON COLUMN chat_action_plans.intent IS 'Classified action intent (e.g. ''proposed_edit'', ''approval_request'').';

COMMENT ON COLUMN chat_action_plans.target_type IS 'Business object type being acted upon.';

COMMENT ON COLUMN chat_action_plans.target_id IS 'UUID of the business object being acted upon.';

COMMENT ON COLUMN chat_action_plans.payload IS 'Full structured action plan JSONB including proposed changes.';

COMMENT ON COLUMN chat_action_plans.confidence IS 'Classifier confidence for the detected intent.';

COMMENT ON COLUMN chat_action_plans.autonomy_mode IS 'Autonomy mode in effect when the action was detected.';

COMMENT ON COLUMN chat_action_plans.status IS 'Review lifecycle state (pending, approved, rejected, superseded, applied).';

COMMENT ON COLUMN chat_action_plans.requires_human_approval IS 'Whether explicit human approval is required before applying.';

COMMENT ON COLUMN chat_action_plans.reasoning IS 'Narrative explanation of the action plan.';

COMMENT ON COLUMN chat_action_plans.applied_result IS 'Result payload when the action was applied.';

COMMENT ON COLUMN chat_action_plans.rejected_reason IS 'Reason provided when the action was rejected.';

COMMENT ON COLUMN chat_action_plans.superseded_by_id IS 'ID of the action plan that superseded this one.';

CREATE INDEX ix_chat_action_plans_thread_status ON chat_action_plans (thread_id, status);

CREATE INDEX ix_chat_action_plans_target ON chat_action_plans (target_type, target_id);

CREATE INDEX ix_chat_action_plans_entity ON chat_action_plans (entity_id, status);

UPDATE alembic_version SET version_num='0012_chat_action_plans' WHERE alembic_version.version_num = '0011_chat_threads_and_messages';

-- Running upgrade 0012_chat_action_plans -> 0013_jobs

CREATE TABLE jobs (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID, 
    close_run_id UUID, 
    document_id UUID, 
    actor_user_id UUID, 
    canceled_by_user_id UUID, 
    resumed_from_job_id UUID, 
    task_name VARCHAR(120) NOT NULL, 
    queue_name VARCHAR(60) NOT NULL, 
    routing_key VARCHAR(160) NOT NULL, 
    status VARCHAR(30) NOT NULL, 
    payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    checkpoint_payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    result_payload JSONB, 
    failure_reason VARCHAR(500), 
    failure_details JSONB, 
    blocking_reason VARCHAR(500), 
    trace_id VARCHAR(64), 
    attempt_count INTEGER DEFAULT 0 NOT NULL, 
    retry_count INTEGER DEFAULT 0 NOT NULL, 
    max_retries INTEGER DEFAULT 0 NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    cancellation_requested_at TIMESTAMP WITH TIME ZONE, 
    canceled_at TIMESTAMP WITH TIME ZONE, 
    dead_lettered_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_jobs PRIMARY KEY (id), 
    CONSTRAINT ck_jobs_jobs_status_valid CHECK (status IN ('queued', 'running', 'blocked', 'failed', 'canceled', 'completed')), 
    CONSTRAINT ck_jobs_jobs_attempt_count_non_negative CHECK (attempt_count >= 0), 
    CONSTRAINT ck_jobs_jobs_retry_count_non_negative CHECK (retry_count >= 0), 
    CONSTRAINT ck_jobs_jobs_max_retries_non_negative CHECK (max_retries >= 0), 
    CONSTRAINT ck_jobs_jobs_retry_count_within_attempts CHECK (retry_count <= attempt_count), 
    CONSTRAINT ck_jobs_jobs_attempt_count_within_retry_budget CHECK (attempt_count <= max_retries + 1), 
    CONSTRAINT ck_jobs_jobs_blocking_reason_matches_status CHECK ((status = 'blocked' AND blocking_reason IS NOT NULL) OR (status <> 'blocked' AND blocking_reason IS NULL)), 
    CONSTRAINT ck_jobs_jobs_dead_letter_requires_failed_status CHECK (dead_lettered_at IS NULL OR status = 'failed'), 
    CONSTRAINT ck_jobs_jobs_canceled_timestamp_requires_canceled_status CHECK (canceled_at IS NULL OR status = 'canceled'), 
    CONSTRAINT fk_jobs_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_jobs_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_jobs_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id), 
    CONSTRAINT fk_jobs_actor_user_id_users FOREIGN KEY(actor_user_id) REFERENCES users (id), 
    CONSTRAINT fk_jobs_canceled_by_user_id_users FOREIGN KEY(canceled_by_user_id) REFERENCES users (id), 
    CONSTRAINT fk_jobs_resumed_from_job_id_jobs FOREIGN KEY(resumed_from_job_id) REFERENCES jobs (id)
);

CREATE INDEX ix_jobs_entity_id_status ON jobs (entity_id, status);

CREATE INDEX ix_jobs_close_run_id_status ON jobs (close_run_id, status);

CREATE INDEX ix_jobs_document_id_status ON jobs (document_id, status);

CREATE INDEX ix_jobs_task_name_status ON jobs (task_name, status);

UPDATE alembic_version SET version_num='0013_jobs' WHERE alembic_version.version_num = '0012_chat_action_plans';

-- Running upgrade 0013_jobs -> 0014_export_distribution_records

CREATE TABLE artifacts (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    report_run_id UUID, 
    artifact_type VARCHAR NOT NULL, 
    storage_key VARCHAR NOT NULL, 
    mime_type VARCHAR NOT NULL, 
    checksum VARCHAR NOT NULL, 
    idempotency_key VARCHAR NOT NULL, 
    version_no INTEGER NOT NULL, 
    released_at TIMESTAMP WITH TIME ZONE, 
    metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_artifacts PRIMARY KEY (id), 
    CONSTRAINT fk_artifacts_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_artifacts_report_run_id_report_runs FOREIGN KEY(report_run_id) REFERENCES report_runs (id), 
    CONSTRAINT ck_artifacts_ck_artifacts_artifact_type_valid CHECK (artifact_type IN ('gl_posting_package', 'report_excel', 'report_pdf', 'audit_trail', 'evidence_pack', 'quickbooks_export')), 
    CONSTRAINT ck_artifacts_ck_artifacts_version_no_positive CHECK (version_no >= 1), 
    CONSTRAINT uq_artifacts_type_idempotency UNIQUE (artifact_type, idempotency_key)
);

CREATE INDEX ix_artifacts_close_run_id ON artifacts (close_run_id);

CREATE INDEX ix_artifacts_artifact_type ON artifacts (artifact_type);

CREATE INDEX ix_artifacts_idempotency_key ON artifacts (idempotency_key);

CREATE INDEX ix_artifacts_close_run_version ON artifacts (close_run_id, version_no);

CREATE TABLE export_runs (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    idempotency_key VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    failure_reason TEXT, 
    artifact_manifest JSONB DEFAULT '[]'::jsonb NOT NULL, 
    evidence_pack_key VARCHAR, 
    triggered_by_user_id UUID, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_export_runs PRIMARY KEY (id), 
    CONSTRAINT fk_export_runs_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_export_runs_triggered_by_user_id_users FOREIGN KEY(triggered_by_user_id) REFERENCES users (id), 
    CONSTRAINT ck_export_runs_ck_export_runs_export_status_valid CHECK (status IN ('pending', 'generating', 'completed', 'failed', 'canceled')), 
    CONSTRAINT ck_export_runs_ck_export_runs_export_version_no_positive CHECK (version_no >= 1), 
    CONSTRAINT uq_export_runs_close_run_idempotency UNIQUE (close_run_id, idempotency_key)
);

CREATE INDEX ix_export_runs_close_run_id ON export_runs (close_run_id);

CREATE INDEX ix_export_runs_status ON export_runs (status);

CREATE INDEX ix_export_runs_idempotency_key ON export_runs (idempotency_key);

CREATE TABLE export_distributions (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    export_run_id UUID NOT NULL, 
    entity_id UUID NOT NULL, 
    close_run_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    recipient_name VARCHAR NOT NULL, 
    recipient_email VARCHAR NOT NULL, 
    recipient_role VARCHAR, 
    delivery_channel VARCHAR NOT NULL, 
    note TEXT, 
    distributed_by_user_id UUID, 
    distributed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_export_distributions PRIMARY KEY (id), 
    CONSTRAINT fk_export_distributions_export_run_id_export_runs FOREIGN KEY(export_run_id) REFERENCES export_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_export_distributions_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id), 
    CONSTRAINT fk_export_distributions_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id), 
    CONSTRAINT fk_export_distributions_distributed_by_user_id_users FOREIGN KEY(distributed_by_user_id) REFERENCES users (id), 
    CONSTRAINT ck_export_distributions_ck_export_distributions_export__fff1 CHECK (delivery_channel IN ('secure_email', 'management_portal', 'board_pack', 'file_share')), 
    CONSTRAINT uq_export_distributions_export_recipient_channel_time UNIQUE (export_run_id, recipient_email, delivery_channel, distributed_at)
);

CREATE INDEX ix_export_distributions_export_run_id ON export_distributions (export_run_id);

CREATE INDEX ix_export_distributions_distributed_at ON export_distributions (distributed_at);

CREATE INDEX ix_export_distributions_close_run_id ON export_distributions (close_run_id);

UPDATE alembic_version SET version_num='0014_export_distribution_records' WHERE alembic_version.version_num = '0013_jobs';

-- Running upgrade 0014_export_distribution_records -> 0015_journal_postings

CREATE TABLE journal_postings (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    journal_entry_id UUID NOT NULL, 
    entity_id UUID NOT NULL, 
    close_run_id UUID NOT NULL, 
    version_no INTEGER NOT NULL, 
    posting_target VARCHAR(40) NOT NULL, 
    provider VARCHAR(60), 
    status VARCHAR(30) DEFAULT 'completed' NOT NULL, 
    artifact_id UUID, 
    artifact_type VARCHAR(60), 
    note TEXT, 
    posting_metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
    posted_by_user_id UUID, 
    posted_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_journal_postings PRIMARY KEY (id), 
    CONSTRAINT fk_journal_postings_journal_entry_id_journal_entries FOREIGN KEY(journal_entry_id) REFERENCES journal_entries (id) ON DELETE CASCADE, 
    CONSTRAINT fk_journal_postings_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE, 
    CONSTRAINT fk_journal_postings_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_journal_postings_artifact_id_artifacts FOREIGN KEY(artifact_id) REFERENCES artifacts (id) ON DELETE SET NULL, 
    CONSTRAINT fk_journal_postings_posted_by_user_id_users FOREIGN KEY(posted_by_user_id) REFERENCES users (id), 
    CONSTRAINT ck_journal_postings_ck_journal_postings_journal_posting_831a CHECK (posting_target IN ('internal_ledger', 'external_erp_package')), 
    CONSTRAINT ck_journal_postings_ck_journal_postings_journal_posting_4275 CHECK (status IN ('completed', 'failed')), 
    CONSTRAINT ck_journal_postings_ck_journal_postings_journal_posting_3df2 CHECK (version_no >= 1), 
    CONSTRAINT ck_journal_postings_ck_journal_postings_journal_posting_d3ff CHECK ((posting_target = 'internal_ledger' AND artifact_id IS NULL) OR (posting_target = 'external_erp_package' AND artifact_id IS NOT NULL)), 
    CONSTRAINT ck_journal_postings_ck_journal_postings_journal_posting_e183 CHECK (provider IS NULL OR provider IN ('generic_erp', 'quickbooks_online')), 
    CONSTRAINT ck_journal_postings_ck_journal_postings_journal_posting_2066 CHECK (artifact_type IS NULL OR artifact_type IN ('gl_posting_package', 'quickbooks_export')), 
    CONSTRAINT uq_journal_postings_journal_entry_id UNIQUE (journal_entry_id)
);

CREATE INDEX ix_journal_postings_close_run_id ON journal_postings (close_run_id);

CREATE INDEX ix_journal_postings_posting_target ON journal_postings (posting_target);

CREATE INDEX ix_journal_postings_status ON journal_postings (status);

UPDATE alembic_version SET version_num='0015_journal_postings' WHERE alembic_version.version_num = '0014_export_distribution_records';

-- Running upgrade 0015_journal_postings -> 0016_supporting_schedules

CREATE TABLE supporting_schedules (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    schedule_type VARCHAR(40) NOT NULL, 
    status VARCHAR(20) DEFAULT 'draft' NOT NULL, 
    note TEXT, 
    reviewed_by_user_id UUID, 
    reviewed_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_supporting_schedules PRIMARY KEY (id), 
    CONSTRAINT fk_supporting_schedules_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_supporting_schedules_reviewed_by_user_id_users FOREIGN KEY(reviewed_by_user_id) REFERENCES users (id), 
    CONSTRAINT ck_supporting_schedules_ck_supporting_schedules_support_ae74 CHECK (schedule_type IN ('fixed_assets', 'loan_amortisation', 'accrual_tracker', 'budget_vs_actual')), 
    CONSTRAINT ck_supporting_schedules_ck_supporting_schedules_support_8702 CHECK (status IN ('draft', 'in_review', 'approved', 'not_applicable')), 
    CONSTRAINT uq_supporting_schedules_close_run_type UNIQUE (close_run_id, schedule_type)
);

CREATE INDEX ix_supporting_schedules_close_run_id ON supporting_schedules (close_run_id);

CREATE INDEX ix_supporting_schedules_close_run_status ON supporting_schedules (close_run_id, status);

CREATE TABLE supporting_schedule_rows (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    supporting_schedule_id UUID NOT NULL, 
    row_ref VARCHAR(200) NOT NULL, 
    line_no INTEGER NOT NULL, 
    payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_supporting_schedule_rows PRIMARY KEY (id), 
    CONSTRAINT fk_supporting_schedule_rows_supporting_schedule_id_supp_d3c7 FOREIGN KEY(supporting_schedule_id) REFERENCES supporting_schedules (id) ON DELETE CASCADE, 
    CONSTRAINT uq_supporting_schedule_rows_schedule_row_ref UNIQUE (supporting_schedule_id, row_ref), 
    CONSTRAINT uq_supporting_schedule_rows_schedule_line_no UNIQUE (supporting_schedule_id, line_no)
);

CREATE INDEX ix_supporting_schedule_rows_schedule_id ON supporting_schedule_rows (supporting_schedule_id);

UPDATE alembic_version SET version_num='0016_supporting_schedules' WHERE alembic_version.version_num = '0015_journal_postings';

-- Running upgrade 0016_supporting_schedules -> 0017_ledger_import_baselines

CREATE TABLE general_ledger_import_batches (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    period_start DATE NOT NULL, 
    period_end DATE NOT NULL, 
    source_format VARCHAR(16) NOT NULL, 
    uploaded_filename VARCHAR(255) NOT NULL, 
    row_count INTEGER NOT NULL, 
    imported_by_user_id UUID, 
    import_metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_general_ledger_import_batches PRIMARY KEY (id), 
    CONSTRAINT ck_general_ledger_import_batches_period_range_valid CHECK (period_end >= period_start), 
    CONSTRAINT ck_general_ledger_import_batches_row_count_positive CHECK (row_count >= 1), 
    CONSTRAINT fk_general_ledger_import_batches_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE, 
    CONSTRAINT fk_general_ledger_import_batches_imported_by_user_id_users FOREIGN KEY(imported_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_general_ledger_import_batches_entity_id ON general_ledger_import_batches (entity_id);

CREATE INDEX ix_gl_import_batches_entity_period ON general_ledger_import_batches (entity_id, period_start, period_end);

CREATE TABLE general_ledger_import_lines (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    batch_id UUID NOT NULL, 
    line_no INTEGER NOT NULL, 
    posting_date DATE NOT NULL, 
    account_code VARCHAR(60) NOT NULL, 
    account_name VARCHAR(255), 
    reference VARCHAR(200), 
    description TEXT, 
    debit_amount NUMERIC(20, 2) NOT NULL, 
    credit_amount NUMERIC(20, 2) NOT NULL, 
    dimensions JSONB DEFAULT '{}'::jsonb NOT NULL, 
    external_ref VARCHAR(120), 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_general_ledger_import_lines PRIMARY KEY (id), 
    CONSTRAINT ck_general_ledger_import_lines_amounts_non_negative CHECK (debit_amount >= 0 AND credit_amount >= 0), 
    CONSTRAINT ck_general_ledger_import_lines_single_sided_amount CHECK ((debit_amount = 0 AND credit_amount > 0) OR (credit_amount = 0 AND debit_amount > 0)), 
    CONSTRAINT fk_general_ledger_import_lines_batch_id_general_ledger__00c8 FOREIGN KEY(batch_id) REFERENCES general_ledger_import_batches (id) ON DELETE CASCADE
);

CREATE INDEX ix_gl_import_lines_batch_date ON general_ledger_import_lines (batch_id, posting_date);

CREATE INDEX ix_gl_import_lines_batch_account ON general_ledger_import_lines (batch_id, account_code);

CREATE TABLE trial_balance_import_batches (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    entity_id UUID NOT NULL, 
    period_start DATE NOT NULL, 
    period_end DATE NOT NULL, 
    source_format VARCHAR(16) NOT NULL, 
    uploaded_filename VARCHAR(255) NOT NULL, 
    row_count INTEGER NOT NULL, 
    imported_by_user_id UUID, 
    import_metadata JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_trial_balance_import_batches PRIMARY KEY (id), 
    CONSTRAINT ck_trial_balance_import_batches_period_range_valid CHECK (period_end >= period_start), 
    CONSTRAINT ck_trial_balance_import_batches_row_count_positive CHECK (row_count >= 1), 
    CONSTRAINT fk_trial_balance_import_batches_entity_id_entities FOREIGN KEY(entity_id) REFERENCES entities (id) ON DELETE CASCADE, 
    CONSTRAINT fk_trial_balance_import_batches_imported_by_user_id_users FOREIGN KEY(imported_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_trial_balance_import_batches_entity_id ON trial_balance_import_batches (entity_id);

CREATE INDEX ix_tb_import_batches_entity_period ON trial_balance_import_batches (entity_id, period_start, period_end);

CREATE TABLE trial_balance_import_lines (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    batch_id UUID NOT NULL, 
    line_no INTEGER NOT NULL, 
    account_code VARCHAR(60) NOT NULL, 
    account_name VARCHAR(255), 
    account_type VARCHAR(80), 
    debit_balance NUMERIC(20, 2) NOT NULL, 
    credit_balance NUMERIC(20, 2) NOT NULL, 
    is_active BOOLEAN DEFAULT true NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_trial_balance_import_lines PRIMARY KEY (id), 
    CONSTRAINT ck_trial_balance_import_lines_balances_non_negative CHECK (debit_balance >= 0 AND credit_balance >= 0), 
    CONSTRAINT ck_trial_balance_import_lines_single_sided_balance CHECK ((debit_balance = 0 AND credit_balance >= 0) OR (credit_balance = 0 AND debit_balance >= 0)), 
    CONSTRAINT fk_trial_balance_import_lines_batch_id_trial_balance_im_e19d FOREIGN KEY(batch_id) REFERENCES trial_balance_import_batches (id) ON DELETE CASCADE
);

CREATE INDEX ix_tb_import_lines_batch_account ON trial_balance_import_lines (batch_id, account_code);

CREATE TABLE close_run_ledger_bindings (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    close_run_id UUID NOT NULL, 
    general_ledger_import_batch_id UUID, 
    trial_balance_import_batch_id UUID, 
    binding_source VARCHAR(16) DEFAULT 'auto' NOT NULL, 
    bound_by_user_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_close_run_ledger_bindings PRIMARY KEY (id), 
    CONSTRAINT ck_close_run_ledger_bindings_binding_source_valid CHECK (binding_source IN ('auto', 'manual')), 
    CONSTRAINT ck_close_run_ledger_bindings_at_least_one_import_required CHECK (general_ledger_import_batch_id IS NOT NULL OR trial_balance_import_batch_id IS NOT NULL), 
    CONSTRAINT uq_close_run_ledger_bindings_close_run_id UNIQUE (close_run_id), 
    CONSTRAINT fk_close_run_ledger_bindings_close_run_id_close_runs FOREIGN KEY(close_run_id) REFERENCES close_runs (id) ON DELETE CASCADE, 
    CONSTRAINT fk_close_run_ledger_bindings_general_ledger_import_batc_194b FOREIGN KEY(general_ledger_import_batch_id) REFERENCES general_ledger_import_batches (id) ON DELETE CASCADE, 
    CONSTRAINT fk_close_run_ledger_bindings_trial_balance_import_batch_cb6d FOREIGN KEY(trial_balance_import_batch_id) REFERENCES trial_balance_import_batches (id) ON DELETE CASCADE, 
    CONSTRAINT fk_close_run_ledger_bindings_bound_by_user_id_users FOREIGN KEY(bound_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_close_run_ledger_bindings_gl_batch ON close_run_ledger_bindings (general_ledger_import_batch_id);

CREATE INDEX ix_close_run_ledger_bindings_tb_batch ON close_run_ledger_bindings (trial_balance_import_batch_id);

UPDATE alembic_version SET version_num='0017_ledger_import_baselines' WHERE alembic_version.version_num = '0016_supporting_schedules';

-- Running upgrade 0017_ledger_import_baselines -> 0018_imported_gl_transaction_group_keys

ALTER TABLE general_ledger_import_lines ADD COLUMN transaction_group_key VARCHAR(40);

UPDATE general_ledger_import_lines
        SET transaction_group_key = 'glgrp_' || md5(
            posting_date::text || '|' ||
            CASE
                WHEN btrim(coalesce(external_ref, '')) <> ''
                    THEN 'external_ref|' || lower(btrim(external_ref))
                WHEN btrim(coalesce(reference, '')) <> ''
                    THEN 'reference|' || lower(btrim(reference))
                WHEN btrim(coalesce(description, '')) <> ''
                    THEN 'description|' || lower(btrim(description))
                ELSE 'line|' || line_no::text
            END
        );

ALTER TABLE general_ledger_import_lines ALTER COLUMN transaction_group_key SET NOT NULL;

CREATE INDEX ix_gl_import_lines_batch_group ON general_ledger_import_lines (batch_id, transaction_group_key);

UPDATE alembic_version SET version_num='0018_imported_gl_transaction_group_keys' WHERE alembic_version.version_num = '0017_ledger_import_baselines';

COMMIT;

