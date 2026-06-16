# GitLab CI Kernel Artifact Upload Design

## Goal

Connect a GitLab kernel build pipeline to this file publishing service so that every successful build can automatically publish `uImage` and `system-top.dtb` to the download site.

The CI job must be able to customize:

- target upload directory
- version string
- changelog or release notes
- Git commit metadata
- CI authentication secret

## Current Context

The existing service already has an authenticated upload endpoint:

- `POST /api/upload`
- fields: `file`, `version`, `changelog`, `git_commit`, `folder_id`
- auth: normal user JWT from `/token`

Files are stored under `DATA_DIR/uploads`, and metadata is stored in SQLite through `FileRecord`. Public downloads are served by Nginx from `/files/<stored_filename>`.

The current upload endpoint is suitable for browser users, but it is awkward for CI because CI would need to log in with a user password and know the numeric `folder_id` in advance.

## Recommended Approach

Add a dedicated CI upload endpoint:

```text
POST /api/ci/upload
```

The endpoint will authenticate with an environment-configured token:

```text
X-CI-Upload-Token: <token>
```

The backend token value comes from:

```text
CI_UPLOAD_TOKEN
```

The GitLab project stores the same value as a masked CI/CD variable:

```text
FILE_UPLOAD_TOKEN
```

This keeps GitLab away from normal user passwords and gives the file service a secret that can be rotated independently.

## Request Fields

The CI endpoint accepts multipart form data:

| Field | Required | Meaning |
| --- | --- | --- |
| `file` | yes | Artifact file to publish |
| `folder_path` | no | Slash-separated path, for example `内核/ZC7015` |
| `version` | no | Version displayed on the download page |
| `changelog` | no | Release notes or commit title |
| `git_commit` | no | GitLab commit SHA or short SHA |
| `submitter` | no | Display submitter, defaults to `gitlab-ci` |

If `folder_path` is empty, the file is uploaded to the root directory. If the path contains missing folders, the backend creates them automatically.

## Folder Path Rules

`folder_path` is split on `/`.

Example:

```text
内核/ZC7015/test
```

The backend resolves or creates:

```text
内核
内核/ZC7015
内核/ZC7015/test
```

Empty path segments are ignored. Leading and trailing slashes are allowed. The endpoint rejects path segments that are empty after trimming or that contain unsafe separators.

## Upload Behavior

The CI endpoint reuses the same persistence behavior as the browser upload:

1. Read the uploaded file.
2. Save it under `UPLOAD_DIR` with a timestamp prefix to avoid physical overwrite.
3. Store a `FileRecord` with original filename, physical path, MD5, version, changelog, Git commit, submitter, folder ID, and file size.
4. Trigger the existing WeChat notification flow when enabled.
5. Return the new file record summary, including ID, filename, folder ID, MD5, and file size.

This preserves the public page behavior: files with the same visible filename and folder are grouped as versions, and the newest version is shown first.

## GitLab CI Configuration

The kernel repository will add an upload step after artifacts are copied to `output_release`.

Recommended variables:

```yaml
variables:
  FILE_SERVER_URL: "https://files.example.internal"
  FILE_RELEASE_FOLDER: "内核/ZC7015"
  FILE_RELEASE_VERSION: "${CI_COMMIT_TAG:-${CI_COMMIT_REF_NAME}-${CI_COMMIT_SHORT_SHA}}"
```

GitLab CI/CD Variables:

```text
FILE_UPLOAD_TOKEN=<same value as backend CI_UPLOAD_TOKEN>
```

Upload script:

```bash
RELEASE_CHANGELOG="${CI_COMMIT_TITLE:-${CI_COMMIT_MESSAGE}}"

for artifact in output_release/uImage output_release/system-top.dtb; do
  curl -f -X POST "$FILE_SERVER_URL/api/ci/upload" \
    -H "X-CI-Upload-Token: $FILE_UPLOAD_TOKEN" \
    -F "file=@${artifact}" \
    -F "folder_path=${FILE_RELEASE_FOLDER}" \
    -F "version=${FILE_RELEASE_VERSION}" \
    -F "changelog=${RELEASE_CHANGELOG}" \
    -F "git_commit=${CI_COMMIT_SHORT_SHA}" \
    -F "submitter=gitlab-ci"
done
```

This upload step should run only after the kernel build and artifact copy commands succeed.

## Docker Configuration

`docker-compose.yml` should pass the CI token to the backend service:

```yaml
environment:
  - CI_UPLOAD_TOKEN=${CI_UPLOAD_TOKEN:?set CI_UPLOAD_TOKEN}
```

For production, the value should be supplied through an environment file or deployment secret rather than committed as a real secret.

## Error Handling

The endpoint returns:

- `401` when the CI token is missing, invalid, or the backend has no `CI_UPLOAD_TOKEN` configured.
- `400` when the folder path is invalid.
- `200` with metadata when upload succeeds.

GitLab uses `curl -f`, so any non-2xx response fails the pipeline and makes upload failures visible.

## Tests

Add backend tests for:

- missing CI token is rejected
- invalid CI token is rejected
- valid token uploads a file
- `folder_path` creates nested folders
- uploaded record stores version, changelog, Git commit, submitter, MD5, file size, and folder ID

Existing stale WeChat tests should be adjusted or removed so the test suite reflects the current webhook-only implementation.

## Out of Scope

- A new release-level database table.
- One request that uploads multiple files at once.
- GitLab API callbacks or job status synchronization.
- Frontend UI changes for CI upload configuration.
