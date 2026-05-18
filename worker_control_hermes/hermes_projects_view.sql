-- hermes_projects_v: backward-compat view that exposes the OLD hermes
-- projects.db column layout (folder_path/display_name/project_type/git_repo/
-- description/learned_notes/use_count/last_used_at) on top of canonical
-- worker_control `projects`. INSTEAD-OF triggers route writes back to the
-- canonical row + the metadata JSON 'hermes' subobject so worker_control
-- stays the single source of truth.

DROP VIEW    IF EXISTS hermes_projects_v;
DROP TRIGGER IF EXISTS hermes_projects_v_insert;
DROP TRIGGER IF EXISTS hermes_projects_v_update;
DROP TRIGGER IF EXISTS hermes_projects_v_delete;

CREATE VIEW hermes_projects_v AS
SELECT
    id,
    path                                                            AS folder_path,
    COALESCE(json_extract(metadata, '$.hermes.project_type'),  '')  AS project_type,
    COALESCE(json_extract(metadata, '$.hermes.git_repo'),
             remote_url)                                            AS git_repo,
    COALESCE(json_extract(metadata, '$.hermes.display_name'), name) AS display_name,
    COALESCE(json_extract(metadata, '$.hermes.description'),  '')   AS description,
    COALESCE(json_extract(metadata, '$.hermes.learned_notes'), '')  AS learned_notes,
    COALESCE(json_extract(metadata, '$.hermes.created_at'),
             created_at)                                            AS created_at,
    COALESCE(json_extract(metadata, '$.hermes.last_used_at'),
             updated_at)                                            AS last_used_at,
    COALESCE(json_extract(metadata, '$.hermes.use_count'),    0)    AS use_count
FROM projects;

CREATE TRIGGER hermes_projects_v_insert
INSTEAD OF INSERT ON hermes_projects_v
FOR EACH ROW
BEGIN
    -- name = folder_path itself (unique by nature; canonical UNIQUE constraint
    -- is satisfied because hermes projects also key on folder_path uniqueness).
    INSERT INTO projects(
        name, path, is_git, branch, remote_url, is_dirty, root_role,
        last_scan_at, metadata, created_at, updated_at
    ) VALUES (
        NEW.folder_path,
        NEW.folder_path,
        CASE WHEN COALESCE(NEW.git_repo, '') <> '' THEN 1 ELSE 0 END,
        NULL,
        NULLIF(NEW.git_repo, ''),
        0,
        CASE
            WHEN lower(replace(NEW.folder_path, '\', '/')) LIKE 'd:/work-github%' THEN 'owned_work'
            WHEN lower(replace(NEW.folder_path, '\', '/')) LIKE 'd:/github%'      THEN 'public_reference'
            ELSE 'other'
        END,
        NULL,
        json_object(
            'hermes',
            json_object(
                'project_type',  COALESCE(NEW.project_type, ''),
                'git_repo',      NEW.git_repo,
                'display_name',  NEW.display_name,
                'description',   COALESCE(NEW.description, ''),
                'learned_notes', COALESCE(NEW.learned_notes, ''),
                'created_at',    COALESCE(NEW.created_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                'last_used_at',  COALESCE(NEW.last_used_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                'use_count',     COALESCE(NEW.use_count, 0)
            )
        ),
        COALESCE(NEW.created_at,   strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        COALESCE(NEW.last_used_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
END;

CREATE TRIGGER hermes_projects_v_update
INSTEAD OF UPDATE ON hermes_projects_v
FOR EACH ROW
BEGIN
    UPDATE projects
       SET path        = NEW.folder_path,
           remote_url  = COALESCE(NULLIF(NEW.git_repo, ''), remote_url),
           is_git      = CASE WHEN COALESCE(NEW.git_repo, '') <> '' THEN 1 ELSE is_git END,
           updated_at  = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
           metadata    = json_set(
                            COALESCE(metadata, '{}'),
                            '$.hermes.project_type',  COALESCE(NEW.project_type,  ''),
                            '$.hermes.git_repo',      NEW.git_repo,
                            '$.hermes.display_name',  NEW.display_name,
                            '$.hermes.description',   COALESCE(NEW.description,   ''),
                            '$.hermes.learned_notes', COALESCE(NEW.learned_notes, ''),
                            '$.hermes.last_used_at',  COALESCE(NEW.last_used_at,
                                                              strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                            '$.hermes.use_count',     COALESCE(NEW.use_count, 0)
                         )
     WHERE id = OLD.id;
END;

CREATE TRIGGER hermes_projects_v_delete
INSTEAD OF DELETE ON hermes_projects_v
FOR EACH ROW
BEGIN
    DELETE FROM projects WHERE id = OLD.id;
END;
