/*
Purpose: Verify the shared review confidence badge renders stable labels and tones.
Scope: Unknown-state handling, score-derived tone mapping, and explicit tone overrides.
Dependencies: Node's built-in test runner, React server rendering, and the built shared UI review badge artifact.
*/

import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ConfidenceBadge,
  deriveConfidenceTone,
  formatConfidenceBadgeLabel,
} from "../../packages/ui/dist/components/review/ConfidenceBadge.js";

test("deriveConfidenceTone maps null confidence to unknown", () => {
  assert.equal(deriveConfidenceTone(null), "unknown");
});

test("deriveConfidenceTone maps bounded scores into the canonical review bands", () => {
  assert.equal(deriveConfidenceTone(0.94), "high");
  assert.equal(deriveConfidenceTone(0.83), "medium");
  assert.equal(deriveConfidenceTone(0.41), "low");
});

test("formatConfidenceBadgeLabel rounds whole percentages and handles unknown values", () => {
  assert.equal(formatConfidenceBadgeLabel(0.876), "88%");
  assert.equal(formatConfidenceBadgeLabel(null), "Unknown");
});

test("ConfidenceBadge renders the derived tone and formatted label", () => {
  const markup = renderToStaticMarkup(<ConfidenceBadge score={0.92} />);

  assert.match(markup, /data-tone="high"/);
  assert.match(markup, />92%</);
  assert.match(markup, /aria-label="Confidence 92%"/);
});

test("ConfidenceBadge respects explicit tone overrides from precomputed queue bands", () => {
  const markup = renderToStaticMarkup(<ConfidenceBadge score={0.92} tone="medium" />);

  assert.match(markup, /data-tone="medium"/);
  assert.match(markup, />92%</);
});
