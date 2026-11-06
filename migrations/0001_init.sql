-- Document Desk schema v1. SQL is the authority for identity, state, attempts, results and the outbox.
-- All timestamps are UTC, defaulted by the database clock.

CREATE TABLE dbo.documents (
    document_id         UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_documents PRIMARY KEY,
    original_filename   NVARCHAR(260)    NOT NULL,
    content_type        VARCHAR(100)     NULL,
    size_bytes          BIGINT           NULL,
    sha256              CHAR(64)         NULL,
    page_count          INT              NULL,
    blob_container      VARCHAR(63)      NOT NULL,
    blob_name           NVARCHAR(400)    NOT NULL,
    blob_etag           VARCHAR(200)     NULL,
    source              VARCHAR(20)      NOT NULL,
    status              VARCHAR(20)      NOT NULL,
    current_attempt_no  INT              NOT NULL CONSTRAINT df_documents_attempt DEFAULT 0,
    category            VARCHAR(50)      NULL,
    current_result_id   UNIQUEIDENTIFIER NULL,
    needs_review        BIT              NOT NULL CONSTRAINT df_documents_review DEFAULT 0,
    retain_until        DATE             NULL,
    last_error_code     VARCHAR(60)      NULL,
    last_error_message  NVARCHAR(500)    NULL,
    created_at          DATETIME2(3)     NOT NULL CONSTRAINT df_documents_created DEFAULT SYSUTCDATETIME(),
    updated_at          DATETIME2(3)     NOT NULL CONSTRAINT df_documents_updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_documents_source CHECK (source IN ('upload','blob_event')),
    CONSTRAINT ck_documents_status CHECK (status IN ('uploading','processing','completed','failed','rejected','upload_failed'))
);
CREATE INDEX ix_documents_created ON dbo.documents (created_at DESC);
CREATE INDEX ix_documents_status ON dbo.documents (status, created_at DESC);
CREATE INDEX ix_documents_category ON dbo.documents (category, created_at DESC);
GO

CREATE TABLE dbo.processing_attempts (
    attempt_id        UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_attempts PRIMARY KEY,
    document_id       UNIQUEIDENTIFIER NOT NULL CONSTRAINT fk_attempts_doc REFERENCES dbo.documents(document_id),
    attempt_no        INT              NOT NULL,
    trigger_kind      VARCHAR(10)      NOT NULL,
    replay_from_stage VARCHAR(10)      NULL,
    replay_reason     NVARCHAR(500)    NULL,
    prior_attempt_id  UNIQUEIDENTIFIER NULL CONSTRAINT fk_attempts_prior REFERENCES dbo.processing_attempts(attempt_id),
    status            VARCHAR(10)      NOT NULL CONSTRAINT df_attempts_status DEFAULT 'active',
    created_at        DATETIME2(3)     NOT NULL CONSTRAINT df_attempts_created DEFAULT SYSUTCDATETIME(),
    completed_at      DATETIME2(3)     NULL,
    CONSTRAINT uq_attempts_doc_no UNIQUE (document_id, attempt_no),
    CONSTRAINT ck_attempts_trigger CHECK (trigger_kind IN ('initial','replay')),
    CONSTRAINT ck_attempts_status CHECK (status IN ('active','succeeded','failed'))
);
GO

CREATE TABLE dbo.stage_states (
    attempt_id              UNIQUEIDENTIFIER NOT NULL CONSTRAINT fk_stage_attempt REFERENCES dbo.processing_attempts(attempt_id),
    stage                   VARCHAR(10)      NOT NULL,
    document_id             UNIQUEIDENTIFIER NOT NULL,
    status                  VARCHAR(12)      NOT NULL,
    tries                   INT              NOT NULL CONSTRAINT df_stage_tries DEFAULT 0,
    max_tries               INT              NOT NULL,
    lease_owner             VARCHAR(100)     NULL,
    lease_expires_at        DATETIME2(3)     NULL,
    next_run_at             DATETIME2(3)     NULL,
    first_started_at        DATETIME2(3)     NULL,
    last_started_at         DATETIME2(3)     NULL,
    completed_at            DATETIME2(3)     NULL,
    carried_from_attempt_id UNIQUEIDENTIFIER NULL,
    last_error_code         VARCHAR(60)      NULL,
    last_error_message      NVARCHAR(500)    NULL,
    created_at              DATETIME2(3)     NOT NULL CONSTRAINT df_stage_created DEFAULT SYSUTCDATETIME(),
    updated_at              DATETIME2(3)     NOT NULL CONSTRAINT df_stage_updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT pk_stage_states PRIMARY KEY (attempt_id, stage),
    CONSTRAINT ck_stage_name CHECK (stage IN ('intake','analyze','index','result')),
    CONSTRAINT ck_stage_status CHECK (status IN ('pending','running','retry_wait','succeeded','failed','carried'))
);
CREATE INDEX ix_stage_running ON dbo.stage_states (status, lease_expires_at) INCLUDE (document_id);
CREATE INDEX ix_stage_doc ON dbo.stage_states (document_id);
GO

CREATE TABLE dbo.stage_tries (
    try_id              BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT pk_stage_tries PRIMARY KEY,
    attempt_id          UNIQUEIDENTIFIER NOT NULL,
    stage               VARCHAR(10)      NOT NULL,
    try_no              INT              NOT NULL,
    document_id         UNIQUEIDENTIFIER NOT NULL,
    worker_id           VARCHAR(100)     NULL,
    message_id          VARCHAR(100)     NULL,
    started_at          DATETIME2(3)     NOT NULL CONSTRAINT df_tries_started DEFAULT SYSUTCDATETIME(),
    finished_at         DATETIME2(3)     NULL,
    outcome             VARCHAR(20)      NULL,
    error_code          VARCHAR(60)      NULL,
    error_message       NVARCHAR(500)    NULL,
    http_status         INT              NULL,
    provider_request_id VARCHAR(100)     NULL,
    CONSTRAINT uq_tries UNIQUE (attempt_id, stage, try_no),
    CONSTRAINT fk_tries_stage FOREIGN KEY (attempt_id, stage) REFERENCES dbo.stage_states(attempt_id, stage),
    CONSTRAINT ck_tries_outcome CHECK (outcome IS NULL OR outcome IN ('succeeded','retryable_error','terminal_error','lease_lost','lease_expired'))
);
CREATE INDEX ix_tries_doc ON dbo.stage_tries (document_id, started_at);
GO

CREATE TABLE dbo.artifacts (
    artifact_id  UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_artifacts PRIMARY KEY,
    document_id  UNIQUEIDENTIFIER NOT NULL CONSTRAINT fk_artifacts_doc REFERENCES dbo.documents(document_id),
    attempt_id   UNIQUEIDENTIFIER NULL,
    stage        VARCHAR(10)      NULL,
    try_no       INT              NULL,
    kind         VARCHAR(20)      NOT NULL,
    container    VARCHAR(63)      NOT NULL,
    blob_name    NVARCHAR(400)    NOT NULL,
    sha256       CHAR(64)         NULL,
    size_bytes   BIGINT           NULL,
    content_type VARCHAR(100)     NULL,
    created_at   DATETIME2(3)     NOT NULL CONSTRAINT df_artifacts_created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_artifacts_kind CHECK (kind IN ('original','provider_response','extracted_text','normalized_result'))
);
CREATE INDEX ix_artifacts_doc ON dbo.artifacts (document_id, kind);
CREATE INDEX ix_artifacts_attempt ON dbo.artifacts (attempt_id, kind);
-- exactly one 'original' artifact row per document, however often intake runs
CREATE UNIQUE INDEX ux_artifacts_original ON dbo.artifacts (document_id) WHERE kind = 'original';
GO

CREATE TABLE dbo.document_results (
    result_id               UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_results PRIMARY KEY,
    document_id             UNIQUEIDENTIFIER NOT NULL CONSTRAINT fk_results_doc REFERENCES dbo.documents(document_id),
    attempt_id              UNIQUEIDENTIFIER NOT NULL CONSTRAINT uq_results_attempt UNIQUE,
    result_schema_version   VARCHAR(30)      NOT NULL,
    category                VARCHAR(50)      NULL,
    classification_status   VARCHAR(30)      NOT NULL,
    category_provider_raw   NVARCHAR(200)    NULL,
    provider_name           VARCHAR(60)      NOT NULL,
    api_version             VARCHAR(20)      NULL,
    analyzer_id             VARCHAR(100)     NULL,
    extractor_analyzer_id   VARCHAR(100)     NULL,
    analyzer_config_version VARCHAR(20)      NULL,
    analyzer_config_hash    VARCHAR(80)      NULL,
    provider_operation_id   VARCHAR(200)     NULL,
    simulated               BIT              NOT NULL CONSTRAINT df_results_sim DEFAULT 0,
    raw_artifact_id         UNIQUEIDENTIFIER NULL,
    text_artifact_id        UNIQUEIDENTIFIER NULL,
    normalized_artifact_id  UNIQUEIDENTIFIER NULL,
    page_count              INT              NULL,
    text_char_count         INT              NULL,
    needs_review            BIT              NOT NULL CONSTRAINT df_results_review DEFAULT 0,
    flags_json              NVARCHAR(MAX)    NULL,
    created_at              DATETIME2(3)     NOT NULL CONSTRAINT df_results_created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT ck_results_flags CHECK (flags_json IS NULL OR ISJSON(flags_json) = 1)
);
CREATE INDEX ix_results_doc ON dbo.document_results (document_id, created_at DESC);
GO

CREATE TABLE dbo.document_fields (
    result_id              UNIQUEIDENTIFIER NOT NULL CONSTRAINT fk_fields_result REFERENCES dbo.document_results(result_id),
    field_name             VARCHAR(64)      NOT NULL,
    value_type             VARCHAR(20)      NOT NULL,
    status                 VARCHAR(10)      NOT NULL,
    value_text             NVARCHAR(MAX)    NULL,   -- complete normalized string value, never truncated
    value_string           NVARCHAR(400)    NULL,   -- bounded search key: first 400 characters of value_text
    value_string_truncated BIT              NOT NULL CONSTRAINT df_fields_trunc DEFAULT 0,
    value_number           DECIMAL(19,4)    NULL,
    value_date             DATE             NULL,
    value_bool             BIT              NULL,
    value_json             NVARCHAR(MAX)    NULL,
    confidence             REAL             NULL,
    source                 NVARCHAR(500)    NULL,
    CONSTRAINT pk_fields PRIMARY KEY (result_id, field_name),
    CONSTRAINT ck_fields_status CHECK (status IN ('present','absent','invalid')),
    CONSTRAINT ck_fields_json CHECK (value_json IS NULL OR ISJSON(value_json) = 1)
);
CREATE INDEX ix_fields_date ON dbo.document_fields (field_name, value_date) WHERE value_date IS NOT NULL;
CREATE INDEX ix_fields_number ON dbo.document_fields (field_name, value_number) WHERE value_number IS NOT NULL;
CREATE INDEX ix_fields_string ON dbo.document_fields (field_name, value_string) WHERE value_string IS NOT NULL;
GO

CREATE TABLE dbo.outbox (
    outbox_id         UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_outbox PRIMARY KEY,
    queue             VARCHAR(60)      NOT NULL,
    body              NVARCHAR(MAX)    NOT NULL,
    document_id       UNIQUEIDENTIFIER NULL,
    attempt_id        UNIQUEIDENTIFIER NULL,
    stage             VARCHAR(10)      NULL,
    not_before        DATETIME2(3)     NOT NULL CONSTRAINT df_outbox_nb DEFAULT SYSUTCDATETIME(),
    created_at        DATETIME2(3)     NOT NULL CONSTRAINT df_outbox_created DEFAULT SYSUTCDATETIME(),
    claimed_by        VARCHAR(100)     NULL,
    claimed_until     DATETIME2(3)     NULL,
    dispatched_at     DATETIME2(3)     NULL,
    dispatch_attempts INT              NOT NULL CONSTRAINT df_outbox_att DEFAULT 0,
    last_error        NVARCHAR(500)    NULL,
    CONSTRAINT ck_outbox_body CHECK (ISJSON(body) = 1)
);
-- keyed on created_at so the dispatcher's ORDER BY is index-supported (READPAST needs this to skip locked rows)
CREATE INDEX ix_outbox_pending ON dbo.outbox (created_at) WHERE dispatched_at IS NULL;
CREATE INDEX ix_outbox_stage ON dbo.outbox (attempt_id, stage, created_at);
GO

CREATE TABLE dbo.worker_heartbeats (
    worker_id    VARCHAR(100) NOT NULL CONSTRAINT pk_heartbeats PRIMARY KEY,
    role         VARCHAR(30)  NOT NULL,
    version      VARCHAR(30)  NULL,
    started_at   DATETIME2(3) NOT NULL,
    last_seen_at DATETIME2(3) NOT NULL
);
GO

CREATE TABLE dbo.dependency_health (
    name       VARCHAR(40)   NOT NULL CONSTRAINT pk_dephealth PRIMARY KEY,
    status     VARCHAR(10)   NOT NULL,
    checked_at DATETIME2(3)  NOT NULL,
    error_class VARCHAR(80)  NULL,
    detail     NVARCHAR(300) NULL
);
GO

-- Replay is a stored procedure so the web identity needs no direct write access to attempt/stage/outbox tables
-- (ownership chaining: procedure and tables are both owned by dbo).
CREATE PROCEDURE dbo.usp_replay_document
    @document_id UNIQUEIDENTIFIER,
    @seen_attempt_no INT,
    @from_stage VARCHAR(10) = NULL,
    @reason NVARCHAR(500) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @stages TABLE (stage VARCHAR(10) PRIMARY KEY, ord INT);
    INSERT @stages VALUES ('intake',1),('analyze',2),('index',3),('result',4);

    BEGIN TRAN;
    DECLARE @prior UNIQUEIDENTIFIER, @new UNIQUEIDENTIFIER = NEWID(), @failed_ord INT, @from_ord INT, @new_no INT = @seen_attempt_no + 1;

    UPDATE dbo.documents SET status = 'processing', current_attempt_no = @new_no, last_error_code = NULL,
           last_error_message = NULL, updated_at = SYSUTCDATETIME()
     WHERE document_id = @document_id AND status = 'failed' AND current_attempt_no = @seen_attempt_no;
    IF @@ROWCOUNT = 0
    BEGIN
        ROLLBACK;
        SELECT CAST('CONFLICT' AS VARCHAR(20)) AS outcome, CAST(NULL AS UNIQUEIDENTIFIER) AS attempt_id, CAST(NULL AS INT) AS attempt_no;
        RETURN;
    END;

    SELECT @prior = attempt_id FROM dbo.processing_attempts WHERE document_id = @document_id AND attempt_no = @seen_attempt_no;
    SELECT TOP (1) @failed_ord = s.ord FROM dbo.stage_states ss JOIN @stages s ON s.stage = ss.stage
     WHERE ss.attempt_id = @prior AND ss.status = 'failed' ORDER BY s.ord;
    SET @failed_ord = COALESCE(@failed_ord, 1);
    SET @from_ord = COALESCE((SELECT ord FROM @stages WHERE stage = @from_stage), @failed_ord);
    IF @from_ord > @failed_ord
    BEGIN
        ROLLBACK;
        SELECT CAST('BAD_STAGE' AS VARCHAR(20)) AS outcome, CAST(NULL AS UNIQUEIDENTIFIER) AS attempt_id, CAST(NULL AS INT) AS attempt_no;
        RETURN;
    END;

    INSERT dbo.processing_attempts (attempt_id, document_id, attempt_no, trigger_kind, replay_from_stage, replay_reason, prior_attempt_id)
    VALUES (@new, @document_id, @new_no, 'replay', (SELECT stage FROM @stages WHERE ord = @from_ord), @reason, @prior);

    -- stages before the replay point are carried forward by reference to the attempt that produced them
    INSERT dbo.stage_states (attempt_id, stage, document_id, status, tries, max_tries, completed_at, carried_from_attempt_id)
    SELECT @new, p.stage, @document_id, 'carried', 0, p.max_tries, SYSUTCDATETIME(),
           CASE WHEN p.status = 'carried' THEN p.carried_from_attempt_id ELSE p.attempt_id END
      FROM dbo.stage_states p JOIN @stages s ON s.stage = p.stage
     WHERE p.attempt_id = @prior AND s.ord < @from_ord AND p.status IN ('succeeded','carried');

    INSERT dbo.stage_states (attempt_id, stage, document_id, status, tries, max_tries)
    SELECT @new, p.stage, @document_id, 'pending', 0, p.max_tries
      FROM dbo.stage_states p JOIN @stages s ON s.stage = p.stage
     WHERE p.attempt_id = @prior AND s.ord = @from_ord;

    INSERT dbo.outbox (outbox_id, queue, body, document_id, attempt_id, stage)
    VALUES (NEWID(), 'stage-' + (SELECT stage FROM @stages WHERE ord = @from_ord),
            CONCAT('{"v":1,"document_id":"', LOWER(CAST(@document_id AS CHAR(36))), '","attempt_id":"', LOWER(CAST(@new AS CHAR(36))),
                   '","stage":"', (SELECT stage FROM @stages WHERE ord = @from_ord), '"}'),
            @document_id, @new, (SELECT stage FROM @stages WHERE ord = @from_ord));
    COMMIT;
    SELECT CAST('OK' AS VARCHAR(20)) AS outcome, @new AS attempt_id, @new_no AS attempt_no;
END;
GO

CREATE ROLE docdesk_web_role;
CREATE ROLE docdesk_worker_role;
GO
GRANT SELECT ON SCHEMA::dbo TO docdesk_web_role;
GRANT INSERT, UPDATE ON dbo.documents TO docdesk_web_role;
GRANT EXECUTE ON dbo.usp_replay_document TO docdesk_web_role;
GRANT SELECT, INSERT, UPDATE ON SCHEMA::dbo TO docdesk_worker_role;
GRANT EXECUTE ON dbo.usp_replay_document TO docdesk_worker_role;
GO
