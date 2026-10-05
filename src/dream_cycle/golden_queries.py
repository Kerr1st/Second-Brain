"""Pure extraction of explicit retrieval questions from memory text."""
def extract_golden_queries(content: str) -> list[str]:
    """Parse 'Questions this answers:' section from insight content.

    Looks for a line starting with 'Questions this answers:' and extracts
    subsequent lines that start with '- ' as individual queries.
    """
    lines = content.split('\n')
    in_section = False
    queries = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith('questions this answers:'):
            in_section = True
            after_colon = stripped.split(':', 1)[1].strip()
            if after_colon:
                queries.append(after_colon)
            continue
        if in_section:
            if stripped.startswith('- '):
                queries.append(stripped[2:].strip())
            elif stripped.startswith('* '):
                queries.append(stripped[2:].strip())
            elif stripped == '':
                if queries:
                    break
            elif queries:
                break
    return queries
