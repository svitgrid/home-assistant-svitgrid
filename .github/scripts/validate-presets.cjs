#!/usr/bin/env node
/**
 * Validates every *.yaml in the given directory against the
 * HaPresetSchema shape contract. Used by .github/workflows/preset-validation.yml
 * to gate PRs that touch presets/.
 *
 * Mirrors the inline validator in svitgrid/scripts/seed-from-yaml.cjs.
 * Keep the two in lockstep — any rule added here must also exist
 * server-side at seed time.
 */
const fs = require('fs');
const path = require('path');
const yaml = require('js-yaml');

function validate(preset, filename) {
  const errors = [];
  const required = ['id', 'version', 'brand', 'model', 'phases', 'hasBattery', 'protocolId', 'entityMap'];
  for (const field of required) {
    if (preset[field] === undefined) errors.push(`missing required field ${field}`);
  }
  if (preset.id !== undefined && !/^[a-z0-9-]+$/.test(preset.id)) {
    errors.push(`id ${JSON.stringify(preset.id)} must match /^[a-z0-9-]+$/`);
  }
  if (preset.version !== undefined && !/^\d+$/.test(String(preset.version))) {
    errors.push(`version must be a numeric string (got ${JSON.stringify(preset.version)})`);
  }
  if (preset.phases !== undefined && ![1, 2, 3].includes(preset.phases)) {
    errors.push(`phases must be 1, 2, or 3 (got ${preset.phases})`);
  }
  if (preset.entityMap !== undefined) {
    if (typeof preset.entityMap !== 'object' || Array.isArray(preset.entityMap)) {
      errors.push(`entityMap must be an object`);
    } else if (Object.keys(preset.entityMap).length === 0) {
      errors.push(`entityMap must have at least one entry`);
    }
  }
  if (preset.protocolId !== undefined &&
      !['home_assistant', 'home_assistant_solarman'].includes(preset.protocolId)) {
    errors.push(`protocolId must be 'home_assistant' or 'home_assistant_solarman'`);
  }
  for (const cmd of preset.commands || []) {
    if (!cmd.id) errors.push(`command missing id`);
    if (cmd.service && typeof cmd.service !== 'string') {
      errors.push(`command ${cmd.id} service must be string`);
    }
    if (cmd.service && !cmd.service.includes('.')) {
      errors.push(`command ${cmd.id} service must be 'domain.name' (got ${JSON.stringify(cmd.service)})`);
    }
    if (cmd.args && (typeof cmd.args !== 'object' || Array.isArray(cmd.args))) {
      errors.push(`command ${cmd.id} args must be an object`);
    }
  }
  if (errors.length > 0) {
    throw new Error(`${filename}:\n  ${errors.join('\n  ')}`);
  }
}

function main() {
  const dir = process.argv[2];
  if (!dir) {
    console.error('Usage: validate-presets.cjs <presets-dir>');
    process.exit(2);
  }
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.yaml') || f.endsWith('.yml'));
  if (files.length === 0) {
    console.log(`No YAML files in ${dir} — nothing to validate.`);
    return;
  }
  let hadError = false;
  for (const file of files) {
    const fullPath = path.join(dir, file);
    try {
      const raw = fs.readFileSync(fullPath, 'utf8');
      const parsed = yaml.load(raw);
      validate(parsed, file);
      console.log(`✓ ${file}`);
    } catch (err) {
      console.error(`✗ ${err.message || err}`);
      hadError = true;
    }
  }
  if (hadError) process.exit(1);
}

main();
