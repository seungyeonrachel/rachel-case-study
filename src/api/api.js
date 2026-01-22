// src/api/api.js

// Create or reuse a session ID per browser tab / session
const getSessionId = () => {
  let id = localStorage.getItem("partselect_session_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("partselect_session_id", id);
  }
  return id;
};

const API_BASE = "http://localhost:8000";

export const getAIMessage = async (userQuery) => {
  try {
    const sessionId = getSessionId();

    const response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-session-id": sessionId, // keeps part/model context
      },
      body: JSON.stringify({ message: userQuery }),
    });

    if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

    // { role: "assistant", content: "...", cards, sources }
    return await response.json();
  } catch (error) {
    console.error("Failed to connect to backend:", error);
    return {
      role: "assistant",
      content:
        "I'm having trouble connecting to the PartSelect expert system right now. Please try again in a moment.",
    };
  }
};

/**
 * SSE streaming over fetch() (POST body supported).
 *
 * Backend should return text/event-stream with lines like:
 *   data: {"type":"delta","delta":"..."}\n\n
 *   data: {"type":"final","role":"assistant","content":"...","cards":[...],"sources":[...]}\n\n
 *   data: [DONE]\n\n
 *
 * @param {string} userQuery
 * @param {(delta: string) => void} onDelta - called repeatedly as text arrives
 * @param {(finalPayload: object|null) => void} onDone - called once at end (optional)
 */
export const streamAIMessage = async (userQuery, onDelta, onDone) => {
  const sessionId = getSessionId();

  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-session-id": sessionId,
      // optional but nice for SSE
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ message: userQuery }),
  });

  if (!response.ok) {
    throw new Error(`HTTP error! status: ${response.status}`);
  }
  if (!response.body) {
    throw new Error("Streaming not supported (no response.body).");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");

  // SSE frames are separated by \n\n; each frame has lines like "data: ..."
  let buffer = "";

  const processFrame = (frame) => {
    const lines = frame.split("\n");
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;

      const data = line.slice(5).trim();

      if (data === "[DONE]") {
        onDone?.(null);
        return "done";
      }

      // Prefer JSON payloads; fall back to raw text
      try {
        const payload = JSON.parse(data);

        if (payload.type === "delta") {
          onDelta?.(payload.delta || "");
        } else if (payload.type === "final") {
          onDone?.(payload);
        } else if (typeof payload.content === "string") {
          // in case you stream full messages
          onDelta?.(payload.content);
        }
      } catch {
        onDelta?.(data);
      }
    }
    return "continue";
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Process complete frames
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);

      const status = processFrame(frame);
      if (status === "done") return;
    }
  }

  // If the server ended without [DONE], still call onDone
  onDone?.(null);
};
