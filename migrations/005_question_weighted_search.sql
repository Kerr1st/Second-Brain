-- Migration 005: Question-aware weighted search vectors
-- Adds extract_questions_text() parser to split "Questions this answers:" section
-- from memory content for weighted tsvector indexing.

-- 1. Questions parser function
CREATE OR REPLACE FUNCTION extract_questions_text(content TEXT)
RETURNS TABLE(questions_text TEXT, remaining_content TEXT)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    lines TEXT[];
    line TEXT;
    i INT;
    n INT;
    header_found BOOLEAN := FALSE;
    in_questions BOOLEAN := FALSE;
    q_lines TEXT[] := '{}';
    r_lines TEXT[] := '{}';
    inline_text TEXT;
    colon_pos INT;
BEGIN
    -- NULL input → ('', '')
    IF content IS NULL THEN
        questions_text := '';
        remaining_content := '';
        RETURN NEXT;
        RETURN;
    END IF;

    lines := string_to_array(content, E'\n');
    n := array_length(lines, 1);

    -- Empty or single-element null array
    IF n IS NULL THEN
        questions_text := '';
        remaining_content := content;
        RETURN NEXT;
        RETURN;
    END IF;

    FOR i IN 1..n LOOP
        line := lines[i];

        IF NOT header_found AND lower(line) LIKE 'questions this answers:%' THEN
            -- Found the header line
            header_found := TRUE;
            in_questions := TRUE;

            -- Keep header line in remaining_content
            r_lines := array_append(r_lines, line);

            -- Extract inline query after the colon
            colon_pos := position(':' IN line);
            IF colon_pos > 0 AND colon_pos < length(line) THEN
                inline_text := trim(substring(line FROM colon_pos + 1));
                IF inline_text <> '' THEN
                    q_lines := array_append(q_lines, inline_text);
                END IF;
            END IF;

        ELSIF in_questions THEN
            -- Inside questions section: collect list items
            IF line LIKE '- %' THEN
                q_lines := array_append(q_lines, substring(line FROM 3));
            ELSIF line LIKE '* %' THEN
                q_lines := array_append(q_lines, substring(line FROM 3));
            ELSE
                -- Empty line or non-list content terminates questions section
                in_questions := FALSE;
                r_lines := array_append(r_lines, line);
            END IF;

        ELSE
            r_lines := array_append(r_lines, line);
        END IF;
    END LOOP;

    questions_text := array_to_string(q_lines, ' ');
    remaining_content := array_to_string(r_lines, E'\n');
    RETURN NEXT;
    RETURN;
END;
$$;

-- 2. Replacement weighted trigger function
CREATE OR REPLACE FUNCTION memories_search_vector_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    q_text TEXT;
    r_content TEXT;
BEGIN
    SELECT eq.questions_text, eq.remaining_content
      INTO q_text, r_content
      FROM extract_questions_text(coalesce(NEW.content, '')) AS eq;

    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.title, '') || ' ' || q_text), 'A')
        || setweight(to_tsvector('english', r_content), 'B');

    RETURN NEW;
END;
$$;

-- 3. Backfill all existing rows with weighted search vectors
UPDATE memories SET search_vector =
    setweight(to_tsvector('english', coalesce(title, '') || ' ' || (SELECT questions_text FROM extract_questions_text(coalesce(content, '')))), 'A')
    || setweight(to_tsvector('english', (SELECT remaining_content FROM extract_questions_text(coalesce(content, '')))), 'B');
