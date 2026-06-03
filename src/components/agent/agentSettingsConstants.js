/**
 * agentSettingsConstants.js — Shared constants for agent settings.
 *
 * Extracted from AgentSettings.jsx so that the component file only exports
 * React components (required for Fast Refresh to work correctly).
 */

export const DEFAULT_SETTINGS = {
  maxRounds:   10,
  model:       'meta/llama-3.1-70b-instruct',
  coderModel:  'meta/codellama-70b-instruct',
  temperature: 0.1,
}
