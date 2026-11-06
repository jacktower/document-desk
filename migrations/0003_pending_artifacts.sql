-- Provider evidence that was received but could not be written to Blob Storage at the moment of failure. The bytes are held
-- durably in SQL (committed with the failure itself) until a reaper writes them to the artifacts container and records the
-- normal artifact row; no new provider call is needed. `content` is cleared once persisted.
CREATE TABLE dbo.pending_artifacts (
    pending_id      UNIQUEIDENTIFIER NOT NULL CONSTRAINT pk_pending_artifacts PRIMARY KEY,
    document_id     UNIQUEIDENTIFIER NOT NULL CONSTRAINT fk_pending_doc REFERENCES dbo.documents(document_id),
    attempt_id      UNIQUEIDENTIFIER NOT NULL,
    stage           VARCHAR(10)      NOT NULL,
    try_no          INT              NOT NULL,
    kind            VARCHAR(20)      NOT NULL,
    container       VARCHAR(63)      NOT NULL,
    blob_name       NVARCHAR(400)    NOT NULL,
    content_type    VARCHAR(100)     NULL,
    sha256          CHAR(64)         NOT NULL,
    size_bytes      BIGINT           NOT NULL,
    provenance_json NVARCHAR(MAX)    NULL,
    content         VARBINARY(MAX)   NULL,
    created_at      DATETIME2(3)     NOT NULL CONSTRAINT df_pending_created DEFAULT SYSUTCDATETIME(),
    persisted_at    DATETIME2(3)     NULL,
    artifact_id     UNIQUEIDENTIFIER NULL,
    attempts        INT              NOT NULL CONSTRAINT df_pending_attempts DEFAULT 0
);
GO
CREATE INDEX ix_pending_artifacts_open ON dbo.pending_artifacts (created_at) WHERE persisted_at IS NULL;
GO
