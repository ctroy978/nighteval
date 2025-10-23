Extract a rubric from the provided text and return **JSON only** in CANONICAL form.

Canonical schema (omit optional fields if unavailable):
{
  "overall_points_possible": number | null,
  "criteria": [
    {
      "id": "snake_case",
      "name": "Label",
      "description": "Short summary (optional)",
      "max_score": number | null,
      "levels": [
        { "name": "Level label", "description": "What this level means", "score": number | string | null }
      ],
      "descriptors": { "4": "string", "3": "string", ... } // optional legacy map
    }
  ]
}

Rules:
- Preserve every criterion described. Generate snake_case IDs when not provided (unique, ≤40 chars).
- Include `levels` with the exact level wording from the rubric. Set `score` to the numeric value if provided; otherwise use the rubric’s label (e.g., "Exceeds Expectations").
- Provide `max_score` only when a numeric top score is explicit (e.g., 4 on a 1–4 scale). Leave it null otherwise.
- If descriptors or performance descriptions exist, include them either in `levels[].description` or in `descriptors` (indexed by the rubric’s own labels).
- When the rubric defines total points, set `overall_points_possible` accordingly; otherwise use `null`.
- Return **JSON only**, no extra prose.

Rubric text:
{{ rubric_text }}
