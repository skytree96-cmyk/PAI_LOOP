// This self-contained function is embedded in the n8n Code nodes. No packages,
// evaluation of schema content, network, or model-output repair are involved.
export function nativeGatewaySchema(original, mode, value) {
  const object = item => item !== null && typeof item === "object" && !Array.isArray(item);
  const fail = () => { throw new Error("NATIVE_SCHEMA_CONTRACT_REJECTED"); };
  const own = (item, key) => Object.hasOwn(item, key);
  const projectedPaths = new Set([
    "#/$defs/EvidenceAnchor/properties/page", "#/$defs/EvidenceAnchor/properties/section",
  ]);
  const removed = new Set(["minimum", "maximum", "exclusiveMinimum", "minLength", "maxLength", "maxItems"]);
  const allowed = new Set(["$defs", "$ref", "type", "properties", "required", "additionalProperties",
    "items", "anyOf", "enum", "title", "description", ...removed]);
  const forbidden = new Set(["__proto__", "prototype", "constructor"]);
  let visited = 0;
  const bounded = depth => { if (++visited > 12000 || depth > 60) fail(); };
  const resolve = reference => {
    if (typeof reference !== "string" || !/^#\/\$defs\/[A-Za-z][A-Za-z0-9_]*$/.test(reference)) fail();
    const key = reference.slice(8);
    if (!object(original.$defs) || !own(original.$defs, key) || !object(original.$defs[key])) fail();
    return original.$defs[key];
  };
  const nullable = node => {
    if (!Array.isArray(node.anyOf) || node.anyOf.length !== 2) fail();
    const nulls = node.anyOf.filter(branch => object(branch) && branch.type === "null" && Object.keys(branch).length === 1);
    const nonnull = node.anyOf.filter(branch => !(object(branch) && branch.type === "null"));
    if (nulls.length !== 1 || nonnull.length !== 1 || !object(nonnull[0])
      || (!own(nonnull[0], "$ref") && !["string", "integer", "number", "boolean", "object", "array"].includes(nonnull[0].type))) fail();
    return nonnull[0];
  };
  const clone = (node, pointer, depth) => {
    bounded(depth);
    if (!object(node) || Object.keys(node).some(key => !allowed.has(key))) fail();
    if ([node.type !== undefined, node.$ref !== undefined, node.anyOf !== undefined].filter(Boolean).length !== 1) fail();
    if (node.$defs !== undefined && pointer !== "#") fail();
    if (node.type !== undefined && !["object", "array", "string", "integer", "number", "boolean", "null"].includes(node.type)) fail();
    if (node.$ref !== undefined) {
      resolve(node.$ref);
      if (Object.keys(node).some(key => !["$ref", "title", "description"].includes(key))) fail();
    }
    if (node.anyOf !== undefined) nullable(node);
    if (projectedPaths.has(pointer)) {
      const branch = nullable(node);
      const expectedType = pointer.endsWith("/page") ? "integer" : "string";
      if (branch.type !== expectedType) fail();
      node = { type: "array", items: branch,
        ...(node.title === undefined ? {} : { title: node.title }),
        description: `${node.description ?? ""} Required transport array: exactly zero or one item. [] explicitly represents null; [value] represents the original non-null value. Never omit this property.` };
    }
    const output = {};
    const annotations = [];
    for (const [key, item] of Object.entries(node)) {
      if (removed.has(key)) {
        if (!Number.isFinite(item) || item < 0) fail();
        annotations.push(`${key}=${item}`);
      } else if (key === "$defs" || key === "properties") {
        if (!object(item)) fail();
        output[key] = {};
        for (const [name, child] of Object.entries(item)) {
          if (forbidden.has(name) || !name.length || name.length > 120) fail();
          output[key][name] = clone(child, `${pointer}/${key}/${name}`, depth + 1);
        }
      } else if (key === "anyOf") output[key] = item.map((child, i) => clone(child, `${pointer}/anyOf/${i}`, depth + 1));
      else if (key === "items") output[key] = clone(item, `${pointer}/items`, depth + 1);
      else if (key === "required") {
        if (!Array.isArray(item) || item.some(name => typeof name !== "string") || new Set(item).size !== item.length) fail();
        output[key] = [...item];
      } else if (key === "enum") {
        if (!Array.isArray(item) || !item.length || item.length > 100 || item.some(entry => typeof entry !== "string")) fail();
        output[key] = [...item];
      } else if (key === "title" || key === "description") {
        if (typeof item !== "string" || item.length > 12000) fail();
        output[key] = item;
      } else output[key] = item;
    }
    if (node.type === "object") {
      if (node.additionalProperties !== false || !object(node.properties) || !Array.isArray(node.required)
        || node.required.some(name => !own(node.properties, name))) fail();
      if (pointer === "#/$defs/EvidenceAnchor" && ["page", "section"].some(name =>
        !own(node.properties, name) || !node.required.includes(name))) fail();
    } else if (node.properties !== undefined || node.required !== undefined || node.additionalProperties !== undefined) fail();
    if (node.type === "array" && !object(node.items)) fail();
    if (annotations.length) output.description = `${output.description ?? ""} Original constraints (validated by the server): ${annotations.join("; ")}.`.trim();
    return output;
  };
  if (!object(original) || original.type !== "object" || JSON.stringify(original).length > 64000) fail();
  const projected = clone(original, "#", 0);
  const checkRefs = (node, stack, depth) => {
    bounded(depth);
    if (node.$ref) {
      if (stack.includes(node.$ref)) fail();
      checkRefs(resolve(node.$ref), [...stack, node.$ref], depth + 1);
    }
    for (const key of ["properties", "$defs"]) if (node[key]) {
      for (const child of Object.values(node[key])) checkRefs(child, stack, depth + 1);
    }
    if (node.items) checkRefs(node.items, stack, depth + 1);
    if (node.anyOf) for (const child of node.anyOf) checkRefs(child, stack, depth + 1);
  };
  checkRefs(original, [], 0);
  const count = expanded => {
    const totals = { unions: 0, optional: 0 };
    const walk = (node, stack, depth) => {
      bounded(depth);
      if (node.$ref !== undefined) {
        if (stack.includes(node.$ref)) fail();
        // Check cycles even in the definition counting pass.
        if (expanded) return walk(projected.$defs[node.$ref.slice(8)], [...stack, node.$ref], depth + 1);
      }
      if (node.anyOf) { totals.unions++; for (const branch of node.anyOf) walk(branch, stack, depth + 1); }
      if (node.properties) {
        totals.optional += Object.keys(node.properties).filter(name => !node.required.includes(name)).length;
        for (const child of Object.values(node.properties)) walk(child, stack, depth + 1);
      }
      if (node.items) walk(node.items, stack, depth + 1);
      if (!expanded && node.$defs) for (const child of Object.values(node.$defs)) walk(child, stack, depth + 1);
    };
    walk(projected, [], 0);
    if (totals.unions > 16 || totals.optional > 24) fail();
    return totals;
  };
  const unique = count(false), expanded = count(true);
  if (JSON.stringify(projected).length > 64000) fail();
  if (mode === "project") return { schema: projected, counts: { unique, expanded } };
  if (mode !== "decode") fail();
  const decode = (data, node, pointer, depth, stack) => {
    bounded(depth);
    if (node.$ref) {
      if (stack.includes(node.$ref)) fail();
      return decode(data, resolve(node.$ref), node.$ref, depth + 1, [...stack, node.$ref]);
    }
    if (projectedPaths.has(pointer)) {
      if (!Array.isArray(data) || data.length > 1 || (data.length === 1 && !own(data, 0))) fail();
      return data.length === 0 ? null : decode(data[0], nullable(node), `${pointer}/transportItem`, depth + 1, stack);
    }
    if (node.anyOf) {
      const branch = nullable(node);
      return data === null ? null : decode(data, branch, `${pointer}/anyOf/nonnull`, depth + 1, stack);
    }
    if (node.type === "object") {
      if (!object(data) || Object.keys(data).some(key => forbidden.has(key) || !own(node.properties, key))) fail();
      const output = {};
      for (const [name, child] of Object.entries(node.properties)) {
        const path = `${pointer}/properties/${name}`;
        if (!own(data, name)) {
          if (node.required.includes(name)) fail();
        } else output[name] = decode(data[name], child, path, depth + 1, stack);
      }
      return output;
    }
    if (node.type === "array") {
      if (!Array.isArray(data)) fail();
      return data.map(item => decode(item, node.items, `${pointer}/items`, depth + 1, stack));
    }
    if ((node.type === "string" && typeof data !== "string")
      || (node.type === "boolean" && typeof data !== "boolean")
      || (node.type === "integer" && !Number.isSafeInteger(data))
      || (node.type === "number" && (typeof data !== "number" || !Number.isFinite(data)))
      || (node.type === "null" && data !== null)) fail();
    if (node.enum && !node.enum.includes(data)) fail();
    return data;
  };
  return decode(value, original, "#", 0, []);
}
