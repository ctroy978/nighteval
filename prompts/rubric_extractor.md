Extract a rubric from the provided text and return **JSON only** in CANONICAL form.

Canonical schema:
{
  "overall_points_possible": int,
  "criteria": [
    { "id": "snake_case", "name": "Label", "max_score": int, "descriptors": { "4": "string", "3": "string", "2": "string", "1": "string" } }
  ]
}

Rules:
- Sum of max_score across criteria MUST equal overall_points_possible unless the rubric text explicitly defines different weighting.
- If levels are described as 1–4, set max_score=4.
- If IDs are not present, generate snake_case IDs from names (unique, ≤40 chars).
- If descriptors exist, include them keyed by strings "4","3","2","1". Otherwise omit.
- Return **JSON only**, no extra prose.

Rubric text:
{{ rubric_text }}
