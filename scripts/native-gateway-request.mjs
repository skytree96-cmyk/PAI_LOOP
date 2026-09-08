export function validateNativeGatewayRequest(json, itemsCount, projectSchema) {
  if (itemsCount !== 1) throw new Error("gateway requires exactly one request item");
  const body = json?.body && typeof json.body === 'object' && !Array.isArray(json.body) ? json.body : null;
  if (!body) throw new Error('request body must be a JSON object');
  const allowedTopLevel = ['input','max_output_tokens','model','service_tier','store','text'];
  const actualTopLevel = Object.keys(body).sort();
  if (actualTopLevel.length !== allowedTopLevel.length || actualTopLevel.some((key, index) => key !== allowedTopLevel[index])) throw new Error('request fields do not match the extraction gateway contract');
  if (body.model !== 'claude-sonnet-5') throw new Error('model must be claude-sonnet-5');
  if (body.service_tier !== 'default' || body.store !== false) throw new Error('service_tier/store contract is invalid');
  if (!Number.isSafeInteger(body.max_output_tokens) || body.max_output_tokens < 256 || body.max_output_tokens > 20000) throw new Error('max_output_tokens is outside the bounded contract');
  if (!Array.isArray(body.input) || body.input.length !== 2) throw new Error('input must contain exactly system and user messages');
  const readMessage = (item, role, maximum) => {
    if (!item || item.role !== role || !Array.isArray(item.content) || item.content.length !== 1) throw new Error('message contract is invalid');
    const part = item.content[0];
    if (!part || part.type !== 'input_text' || typeof part.text !== 'string') throw new Error('content must be one input_text part');
    const value = part.text;
    if (!value.trim() || value.length > maximum || /[\u0000]/.test(value)) throw new Error('content is empty, oversized, or unsafe');
    return value;
  };
  const systemPrompt = readMessage(body.input[0], 'system', 12000);
  const userPrompt = readMessage(body.input[1], 'user', 140000);
  if (!systemPrompt.startsWith('You extract procurement requirements as evidence only.')) throw new Error('system prompt identity is invalid');
  const initialPrompt = userPrompt.startsWith('Allowed attachment IDs:');
  const correctivePrompt = userPrompt.startsWith('FINAL CORRECTIVE RETRY.') && userPrompt.includes('Allowed attachment IDs:');
  if ((!initialPrompt && !correctivePrompt) || !userPrompt.includes('\n\nSOURCE:\n')) throw new Error('user prompt identity is invalid');
  const format = body.text?.format;
  if (!format || format.type !== 'json_schema' || format.name !== 'pai_loop_requirements' || format.strict !== true) throw new Error('strict response format is invalid');
  const schema = format.schema;
  if (!schema || typeof schema !== 'object' || Array.isArray(schema) || schema.type !== 'object' || schema.additionalProperties !== false || !schema.properties || typeof schema.properties !== 'object' || !Array.isArray(schema.required)) throw new Error('response JSON schema boundary is invalid');
  const schemaJson = JSON.stringify(schema);
  if (!schemaJson || schemaJson.length > 64000) throw new Error('response JSON schema is oversized');
  const projection = projectSchema(schema, 'project');
  const convention = '\n\nTRUSTED NATIVE TRANSPORT CONVENTION: Only the two top-level fields quantitative_tables and quantitative_table_not_applicable are JSON strings encoding their complete original values. For no quantitative tables return the string "[]"; for no not-applicable statement return the string "null". Otherwise encode the complete original array/object as strict JSON text inside its string. Never omit either field, use Markdown, duplicate object keys, trailing text, or change any nested type, field, enum, value or evidence. EvidenceAnchor.page and section retain their ORIGINAL required integer-or-null/string-or-null forms, not arrays and never omitted. This overrides only the two top-level field types in the original schema. The server strictly decodes those two strings and validates the entire original schema and source evidence.';
  const schemaInstruction = `\n\nReturn one JSON object under the trusted two-field JSON string transport convention. No Markdown. ORIGINAL RESPONSE JSON SCHEMA:\n${schemaJson}`;
  const providerSystem = systemPrompt + convention;
  if (providerSystem.length > 12000) throw new Error('combined system prompt is oversized');
  const combinedCharacters = providerSystem.length + userPrompt.length + schemaInstruction.length + JSON.stringify(projection.schema).length;
  if (combinedCharacters > 210000) throw new Error('combined Claude request is oversized');
  return [{ json: { original_schema: schema, provider_request: {
    model: 'claude-sonnet-5', max_tokens: body.max_output_tokens,
    system: providerSystem, messages: [{ role: 'user', content: userPrompt + schemaInstruction }],
    thinking: { type: 'adaptive' }, output_config: { effort: 'medium', format: { type: 'json_schema', schema: projection.schema } },
    stream: false,
  } } }];
}
