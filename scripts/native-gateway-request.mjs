export function validateNativeGatewayRequest(json, itemsCount, projectSchema) {
  if (itemsCount !== 1) throw new Error("INPUT_ITEM_COUNT_INVALID");
  const body = json?.body && typeof json.body === 'object' && !Array.isArray(json.body) ? json.body : null;
  if (!body) throw new Error('INPUT_BODY_INVALID');
  const allowedTopLevel = ['input','max_output_tokens','model','service_tier','store','text'];
  const longOutputOnce = body.budget_policy === 'LONG_OUTPUT_ONCE';
  const quantitativeProbeOnce = body.budget_policy === 'QUANTITATIVE_PROBE_ONCE';
  if (Object.hasOwn(body, 'budget_policy')) {
    if ((!longOutputOnce && !quantitativeProbeOnce)
      || body.max_output_tokens !== (longOutputOnce ? 32000 : 20000)) throw new Error('INPUT_BUDGET_POLICY_INVALID');
    allowedTopLevel.push('budget_policy');
    if (quantitativeProbeOnce) {
      // Scope is an explicit request contract, never inferred from source or
      // prompt prose. Only this diagnostic policy may carry the scope field.
      if (body.request_scope !== 'QUANTITATIVE_PROBE_ONLY') throw new Error('INPUT_PROBE_SCOPE_INVALID');
      allowedTopLevel.push('request_scope');
    }
    allowedTopLevel.sort();
  }
  const actualTopLevel = Object.keys(body).sort();
  if (actualTopLevel.length !== allowedTopLevel.length || actualTopLevel.some((key, index) => key !== allowedTopLevel[index])) throw new Error('INPUT_FIELDS_INVALID');
  if (body.model !== 'claude-sonnet-5') throw new Error('INPUT_MODEL_INVALID');
  if (body.service_tier !== 'default' || body.store !== false) throw new Error('INPUT_OPTIONS_INVALID');
  if (!Number.isSafeInteger(body.max_output_tokens) || body.max_output_tokens < 256 || body.max_output_tokens > (longOutputOnce ? 32000 : 20000)) throw new Error('INPUT_TOKEN_LIMIT_INVALID');
  if (!Array.isArray(body.input) || body.input.length !== 2) throw new Error('INPUT_MESSAGES_INVALID');
  const readMessage = (item, role, maximum) => {
    if (!item || item.role !== role || !Array.isArray(item.content) || item.content.length !== 1) throw new Error('INPUT_MESSAGE_SHAPE_INVALID');
    const part = item.content[0];
    if (!part || part.type !== 'input_text' || typeof part.text !== 'string') throw new Error('INPUT_CONTENT_TYPE_INVALID');
    const value = part.text;
    if (!value.trim()) throw new Error('INPUT_CONTENT_EMPTY');
    if (value.length > maximum) throw new Error(role === 'system' ? 'INPUT_SYSTEM_TOO_LARGE' : 'INPUT_SOURCE_TOO_LARGE');
    if (/[\u0000]/.test(value)) throw new Error('INPUT_CONTENT_NUL');
    return value;
  };
  const systemPrompt = readMessage(body.input[0], 'system', 12000);
  const userPrompt = readMessage(body.input[1], 'user', 140000);
  if (!systemPrompt.startsWith('You extract procurement requirements as evidence only.')) throw new Error('INPUT_SYSTEM_IDENTITY_INVALID');
  const initialPrompt = userPrompt.startsWith('Allowed attachment IDs:');
  const correctivePrompt = userPrompt.startsWith('FINAL CORRECTIVE RETRY.') && userPrompt.includes('Allowed attachment IDs:');
  if ((longOutputOnce || quantitativeProbeOnce) && !initialPrompt) throw new Error('INPUT_CORRECTIVE_POLICY_INVALID');
  if ((!initialPrompt && !correctivePrompt) || !userPrompt.includes('\n\nSOURCE:\n')) throw new Error('INPUT_USER_IDENTITY_INVALID');
  const format = body.text?.format;
  if (!format || format.type !== 'json_schema' || format.name !== 'pai_loop_requirements' || format.strict !== true) throw new Error('INPUT_FORMAT_INVALID');
  const schema = format.schema;
  if (!schema || typeof schema !== 'object' || Array.isArray(schema) || schema.type !== 'object' || schema.additionalProperties !== false || !schema.properties || typeof schema.properties !== 'object' || !Array.isArray(schema.required)) throw new Error('INPUT_SCHEMA_INVALID');
  const schemaJson = JSON.stringify(schema);
  if (!schemaJson || schemaJson.length > 64000) throw new Error('INPUT_SCHEMA_TOO_LARGE');
  let projection;
  try { projection = projectSchema(schema, 'project'); }
  catch (ignored) { throw new Error('INPUT_SCHEMA_PROJECTION_REJECTED'); }
  const convention = '\n\nTRUSTED NATIVE TRANSPORT CONVENTION: Only the two top-level fields quantitative_tables and quantitative_table_not_applicable are JSON strings encoding their complete original values. For no quantitative tables return the string "[]"; for no not-applicable statement return the string "null". Otherwise encode the complete original array/object as strict JSON text inside its string. Never omit either field, use Markdown, duplicate object keys, trailing text, or change any nested type, field, enum, value or evidence. EvidenceAnchor.page and section retain their ORIGINAL required integer-or-null/string-or-null forms, not arrays and never omitted. This overrides only the two top-level field types in the original schema. The server strictly decodes those two strings and validates the entire original schema and source evidence.';
  const schemaInstruction = `\n\nReturn one JSON object under the trusted two-field JSON string transport convention. No Markdown. ORIGINAL RESPONSE JSON SCHEMA:\n${schemaJson}`;
  const providerSystem = systemPrompt + convention;
  if (providerSystem.length > 12000) throw new Error('INPUT_PROVIDER_SYSTEM_TOO_LARGE');
  const combinedCharacters = providerSystem.length + userPrompt.length + schemaInstruction.length + JSON.stringify(projection.schema).length;
  if (combinedCharacters > 210000) throw new Error('INPUT_REQUEST_TOO_LARGE');
  // Derived transport metadata stays outside the Anthropic request. The body
  // allowlist forbids callers from supplying an arbitrary timeout or retry cap.
  const gatewayTimeoutMs = longOutputOnce || quantitativeProbeOnce ? 300000 : 180000;
  return [{ json: { original_schema: schema, gateway_timeout_ms: gatewayTimeoutMs, provider_request: {
    model: 'claude-sonnet-5', max_tokens: body.max_output_tokens,
    system: providerSystem, messages: [{ role: 'user', content: userPrompt + schemaInstruction }],
    thinking: { type: 'adaptive' }, output_config: { effort: 'medium', format: { type: 'json_schema', schema: projection.schema } },
    stream: false,
  } } }];
}
