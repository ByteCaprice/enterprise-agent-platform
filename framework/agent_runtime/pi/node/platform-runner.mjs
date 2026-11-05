// Governed Pi runtime profile. No provider transport, CLI, discovery or business credentials.
import { Agent } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { readFile } from "node:fs/promises";
import { createInterface } from "node:readline";

const PROFILE = "enterprise-ai.pi.v1";
const MAX_FRAME = 2 * 1024 * 1024;
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 19)) throw new Error("unsupported_node");
for (const name of ["pi-agent-core", "pi-ai"]) {
  const info = JSON.parse(await readFile(new URL(`./node_modules/@earendil-works/${name}/package.json`, import.meta.url), "utf8"));
  if (info.version !== "1.4.0") throw new Error("unsupported_pi");
}

// Defense in depth, not an OS network sandbox. The streamFn never calls fetch.
globalThis.fetch = async () => { throw new Error("provider_transport_disabled"); };

const ready = { type: "ready", profile: PROFILE, pi_agent_core: "1.4.0", pi_ai: "1.4.0" };
if (process.argv.includes("--check")) {
  process.stdout.write(`${JSON.stringify(ready)}\n`);
} else {
  await serve();
}

async function serve() {
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let agent;
  let started = false;
  let finished = false;
  let aborted = false;
  let failed = false;
  let sequence = 0;
  let turns = 0;
  let toolCalls = 0;
  let pending;

  function send(frame) {
    const line = `${JSON.stringify({ profile: PROFILE, ...frame })}\n`;
    if (Buffer.byteLength(line) > MAX_FRAME) return Promise.reject(new Error("frame_limit"));
    return new Promise((resolve, reject) => process.stdout.write(line, (error) => error ? reject(error) : resolve()));
  }

  function abort() {
    aborted = true;
    agent?.abort();
    pending?.reject(new Error("aborted"));
    pending = undefined;
  }

  async function request(operation, data = {}) {
    if (aborted || failed || pending) throw new Error("invalid_request_state");
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      pending = { id, resolve, reject };
      send({ type: "request", id, operation, ...data }).catch((error) => {
        pending = undefined;
        reject(error);
      });
    });
  }

  // Pi requires numeric Usage for its in-memory message shape. These placeholders
  // never cross the wire or enter billing; Python records actual/unknown Usage.
  const displayUsage = () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } });

  function streamTurn(model, _context, options = {}) {
    const stream = createAssistantMessageEventStream();
    const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
      content: [], usage: displayUsage(), stopReason: "pending", timestamp: Date.now() };
    void (async () => {
      try {
        options.signal?.throwIfAborted();
        if (turns >= config.max_turns) throw new Error("turn_limit");
        turns++;
        const turn = await request("model");
        options.signal?.throwIfAborted();
        if (!turn || !["final", "tools"].includes(turn.kind) || typeof turn.text !== "string" ||
            !Array.isArray(turn.tool_calls) || (turn.kind === "tools") !== Boolean(turn.tool_calls.length)) {
          throw new Error("invalid_turn");
        }
        if (toolCalls + turn.tool_calls.length > config.max_tool_calls) throw new Error("tool_limit");
        stream.push({ type: "start", partial: message });
        if (turn.text) {
          const contentIndex = message.content.length;
          const block = { type: "text", text: "" };
          message.content.push(block);
          stream.push({ type: "text_start", contentIndex, partial: message });
          block.text = turn.text;
          stream.push({ type: "text_delta", contentIndex, delta: turn.text, partial: message });
          stream.push({ type: "text_end", contentIndex, content: turn.text, partial: message });
        }
        for (const call of turn.tool_calls) {
          if (!call || typeof call.dispatch_id !== "string" || !toolNames.has(call.alias) ||
              !call.arguments || typeof call.arguments !== "object" || Array.isArray(call.arguments)) {
            throw new Error("invalid_tool_call");
          }
          const contentIndex = message.content.length;
          const block = { type: "toolCall", id: call.dispatch_id, name: call.alias, arguments: {} };
          message.content.push(block);
          stream.push({ type: "toolcall_start", contentIndex, partial: message });
          block.arguments = call.arguments;
          stream.push({ type: "toolcall_delta", contentIndex, delta: JSON.stringify(call.arguments), partial: message });
          stream.push({ type: "toolcall_end", contentIndex, toolCall: block, partial: message });
        }
        message.stopReason = turn.kind === "tools" ? "toolUse" : "stop";
        stream.push({ type: "done", reason: message.stopReason, message });
      } catch {
        failed = true;
        message.stopReason = aborted || options.signal?.aborted ? "aborted" : "error";
        message.errorMessage = "governed_turn_failed";
        stream.push({ type: "error", reason: message.stopReason, error: message });
      } finally {
        stream.end();
      }
    })();
    return stream;
  }

  let config;
  let toolNames;
  async function run(start) {
    config = start;
    if (typeof config.instructions !== "string" || typeof config.input_text !== "string" ||
        !Array.isArray(config.tools) || config.tools.length > 64 ||
        !Number.isInteger(config.max_turns) || config.max_turns < 1 || config.max_turns > 128 ||
        !Number.isInteger(config.max_tool_calls) || config.max_tool_calls < 0 || config.max_tool_calls > 512) {
      throw new Error("invalid_start");
    }
    toolNames = new Set(config.tools.map((tool) => tool.alias));
    if (toolNames.size !== config.tools.length) throw new Error("duplicate_tool");
    const tools = config.tools.map((tool) => ({
      name: tool.alias, label: tool.alias, description: tool.description, parameters: tool.input_schema,
      execute: async (dispatchId, _args, signal) => {
        try {
          signal?.throwIfAborted();
          if (++toolCalls > config.max_tool_calls) throw new Error("tool_limit");
          const result = await request("tool", { dispatch_id: dispatchId });
          signal?.throwIfAborted();
          return { content: [{ type: "text", text: JSON.stringify(result) }], details: {} };
        } catch {
          failed = true;
          agent.abort();
          throw new Error("governed_tool_failed");
        }
      },
    }));
    // Virtual descriptor only: no actual provider/model selection or endpoint leaves Python.
    const model = { id: "governed-turn", name: "Governed turn", api: "enterprise-ai-broker", provider: "enterprise-ai",
      baseUrl: "", reasoning: false, input: ["text"], contextWindow: 262144, maxTokens: 262144,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    agent = new Agent({
      initialState: { model, systemPrompt: config.instructions, tools, thinkingLevel: "off" },
      streamFn: streamTurn, toolExecution: "sequential", identity: { attribution: null },
      finishTurn: ({ message, toolResults }) => {
        if (failed || aborted || ["error", "aborted", "length"].includes(message.stopReason) ||
            toolResults.some((result) => result.isError)) {
          failed = true;
          return { action: "end" };
        }
      },
    });
    await agent.prompt(config.input_text);
    const last = agent.state.messages.findLast((message) => message.role === "assistant");
    if (failed || aborted || pending || !last || last.stopReason !== "stop") throw new Error("incomplete_run");
    const finalText = last.content.filter((block) => block.type === "text").map((block) => block.text).join("");
    if (!finalText.trim()) throw new Error("empty_final");
    await send({ type: "result", final_text: finalText, turns, tool_calls: toolCalls });
  }

  async function complete(start) {
    try {
      await run(start);
    } catch {
      await send({ type: "error", code: aborted ? "aborted" : "runtime_failed" });
    } finally {
      finished = true;
      abort();
      input.close();
      process.stdin.pause();
    }
  }

  input.on("line", (line) => {
    try {
      if (Buffer.byteLength(line) > MAX_FRAME) throw new Error("frame_limit");
      const frame = JSON.parse(line);
      if (frame.profile !== PROFILE) throw new Error("invalid_profile");
      if (frame.type === "start" && !started) {
        started = true;
        void complete(frame);
      } else if (frame.type === "response" && pending && frame.id === pending.id) {
        const current = pending;
        pending = undefined;
        current.resolve(frame.value);
      } else if (frame.type === "abort") {
        abort();
      } else {
        throw new Error("invalid_frame");
      }
    } catch {
      failed = true;
      abort();
      process.exitCode = 1;
      input.close();
      process.stdin.pause();
    }
  });
  input.on("close", () => { if (!finished) abort(); });
  process.on("SIGTERM", abort);
  process.on("SIGINT", abort);
  await send(ready);
}
