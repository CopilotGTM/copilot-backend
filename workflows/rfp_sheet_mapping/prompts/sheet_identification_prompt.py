COLUMN_IDENTIFICATION_PROMPT = """
You are an expert at analyzing Excel sheets and identifying column names.
Your task is to analyze the provided sheet content and identify the actual column names.

Important considerations:
1. Column names might not be in the first row
2. The first row might contain:
   - Images
   - Sheet headers/descriptions
   - Other non-column content
3. Column names might be in the second row or later
4. Some rows might contain descriptions or headers that look like column names but aren't

For each column, provide:
1. The column name

Be thorough in identifying the true column names, even if they're not in the first row.
"""
