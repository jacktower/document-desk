-- Analysis provenance is recorded with the artifact that holds the provider response, so indexing (and any later replay of
-- indexing) attributes the response to the configuration that actually produced it, not to whatever the indexing worker runs.
-- Also adds an artifact kind for the final response of a failed or unreadable provider operation.
ALTER TABLE dbo.artifacts ADD provenance_json NVARCHAR(MAX) NULL;
GO
ALTER TABLE dbo.artifacts ADD CONSTRAINT ck_artifacts_prov CHECK (provenance_json IS NULL OR ISJSON(provenance_json) = 1);
GO
ALTER TABLE dbo.artifacts DROP CONSTRAINT ck_artifacts_kind;
GO
ALTER TABLE dbo.artifacts ADD CONSTRAINT ck_artifacts_kind
    CHECK (kind IN ('original','provider_response','provider_error','extracted_text','normalized_result'));
GO
