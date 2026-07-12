export type CodeLanguage = "http" | "shell" | "python" | "typescript" | "javascript";

interface HighlightSegment {
  readonly text: string;
  readonly kind:
    | "plain"
    | "keyword"
    | "string"
    | "variable"
    | "property"
    | "number"
    | "comment"
    | "command";
}

const PYTHON_KEYWORDS = new Set(["def", "from", "import", "return"]);
const JAVASCRIPT_KEYWORDS = new Set([
  "async",
  "await",
  "const",
  "export",
  "from",
  "import",
  "let",
  "new",
  "return",
  "type",
]);
const SHELL_COMMANDS = new Set(["curl", "export"]);
const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

function findStringEnd(line: string, start: number, quote: string): number {
  let index = start + 1;
  while (index < line.length) {
    if (line[index] === "\\" && index + 1 < line.length) {
      index += 2;
      continue;
    }
    if (line[index] === quote) {
      return index + 1;
    }
    index += 1;
  }
  return line.length;
}

function highlightPythonLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;
  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);
    if (char === "#") {
      segments.push({ text: rest, kind: "comment" });
      break;
    }
    if (char === '"' || char === "'") {
      const end = findStringEnd(line, index, char);
      segments.push({ text: line.slice(index, end), kind: "string" });
      index = end;
      continue;
    }
    const envMatch = rest.match(/^[A-Z][A-Z0-9_]+/);
    if (envMatch !== null) {
      segments.push({ text: envMatch[0], kind: "variable" });
      index += envMatch[0].length;
      continue;
    }
    const wordMatch = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (wordMatch !== null) {
      const word = wordMatch[0];
      segments.push({
        text: word,
        kind: PYTHON_KEYWORDS.has(word) ? "keyword" : "plain",
      });
      index += word.length;
      continue;
    }
    const numberMatch = rest.match(/^\d+(?:\.\d+)?/);
    if (numberMatch !== null) {
      segments.push({ text: numberMatch[0], kind: "number" });
      index += numberMatch[0].length;
      continue;
    }
    segments.push({ text: char, kind: "plain" });
    index += 1;
  }
  return segments;
}

function highlightShellLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;
  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);
    if (char === "#") {
      segments.push({ text: rest, kind: "comment" });
      break;
    }
    if (char === '"' || char === "'") {
      const end = findStringEnd(line, index, char);
      segments.push({ text: line.slice(index, end), kind: "string" });
      index = end;
      continue;
    }
    const variableMatch = rest.match(/^\$[A-Z][A-Z0-9_]*/);
    if (variableMatch !== null) {
      segments.push({ text: variableMatch[0], kind: "variable" });
      index += variableMatch[0].length;
      continue;
    }
    const envMatch = rest.match(/^[A-Z][A-Z0-9_]+(?==)/);
    if (envMatch !== null) {
      segments.push({ text: envMatch[0], kind: "variable" });
      index += envMatch[0].length;
      continue;
    }
    const flagMatch = rest.match(/^-[A-Za-z]/);
    if (flagMatch !== null) {
      segments.push({ text: flagMatch[0], kind: "property" });
      index += flagMatch[0].length;
      continue;
    }
    const wordMatch = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (wordMatch !== null) {
      const word = wordMatch[0];
      segments.push({
        text: word,
        kind: SHELL_COMMANDS.has(word) ? "command" : "plain",
      });
      index += word.length;
      continue;
    }
    segments.push({ text: char, kind: "plain" });
    index += 1;
  }
  return segments;
}

function highlightJavaScriptLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;
  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);
    if (rest.startsWith("//")) {
      segments.push({ text: rest, kind: "comment" });
      break;
    }
    if (char === '"' || char === "'" || char === "`") {
      const end = findStringEnd(line, index, char);
      segments.push({ text: line.slice(index, end), kind: "string" });
      index = end;
      continue;
    }
    const envMatch = rest.match(/^[A-Z][A-Z0-9_]+/);
    if (envMatch !== null) {
      segments.push({ text: envMatch[0], kind: "variable" });
      index += envMatch[0].length;
      continue;
    }
    const propertyMatch = rest.match(/^[A-Za-z_][A-Za-z0-9_]*(?=\s*:)/);
    if (propertyMatch !== null) {
      segments.push({ text: propertyMatch[0], kind: "property" });
      index += propertyMatch[0].length;
      continue;
    }
    const wordMatch = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (wordMatch !== null) {
      const word = wordMatch[0];
      segments.push({
        text: word,
        kind: JAVASCRIPT_KEYWORDS.has(word) ? "keyword" : "plain",
      });
      index += word.length;
      continue;
    }
    const numberMatch = rest.match(/^\d+(?:\.\d+)?/);
    if (numberMatch !== null) {
      segments.push({ text: numberMatch[0], kind: "number" });
      index += numberMatch[0].length;
      continue;
    }
    segments.push({ text: char, kind: "plain" });
    index += 1;
  }
  return segments;
}

function highlightJsonishLine(line: string): ReadonlyArray<HighlightSegment> {
  const segments: HighlightSegment[] = [];
  let index = 0;
  while (index < line.length) {
    const char = line[index] ?? "";
    const rest = line.slice(index);
    const placeholderMatch = rest.match(/^\{\{[A-Z0-9_]+\}\}/);
    if (placeholderMatch !== null) {
      segments.push({ text: placeholderMatch[0], kind: "variable" });
      index += placeholderMatch[0].length;
      continue;
    }
    if (char === '"') {
      const end = findStringEnd(line, index, '"');
      const next = line.slice(end).trimStart();
      segments.push({
        text: line.slice(index, end),
        kind: next.startsWith(":") ? "property" : "string",
      });
      index = end;
      continue;
    }
    const numberMatch = rest.match(/^\d+(?:\.\d+)?/);
    if (numberMatch !== null) {
      segments.push({ text: numberMatch[0], kind: "number" });
      index += numberMatch[0].length;
      continue;
    }
    segments.push({ text: char, kind: "plain" });
    index += 1;
  }
  return segments;
}

function highlightHttpLine(line: string): ReadonlyArray<HighlightSegment> {
  const method = line.split(" ", 1)[0] ?? "";
  if (HTTP_METHODS.has(method)) {
    return [
      { text: method, kind: "keyword" },
      { text: line.slice(method.length), kind: "string" },
    ];
  }
  const headerMatch = line.match(/^([A-Za-z-]+:)(.*)$/);
  if (headerMatch !== null) {
    return [
      { text: headerMatch[1] ?? "", kind: "property" },
      { text: headerMatch[2] ?? "", kind: "plain" },
    ];
  }
  return highlightJsonishLine(line);
}

function highlightLine(
  line: string,
  language: CodeLanguage,
): ReadonlyArray<HighlightSegment> {
  if (language === "python") {
    return highlightPythonLine(line);
  }
  if (language === "shell") {
    return highlightShellLine(line);
  }
  if (language === "typescript" || language === "javascript") {
    return highlightJavaScriptLine(line);
  }
  return highlightHttpLine(line);
}

export function SyntaxCode({
  code,
  language,
}: {
  readonly code: string;
  readonly language: CodeLanguage;
}): JSX.Element {
  return (
    <code>
      {code.split("\n").map((line, lineIndex) => (
        <span className="snippet__line" key={`${lineIndex}-${line}`}>
          {highlightLine(line, language).map((segment, segmentIndex) => (
            <span
              className={`snippet__token snippet__token--${segment.kind}`}
              key={`${lineIndex}-${segmentIndex}-${segment.text}`}
            >
              {segment.text}
            </span>
          ))}
        </span>
      ))}
    </code>
  );
}
