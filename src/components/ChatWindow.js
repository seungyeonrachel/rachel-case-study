import React, { useState, useEffect, useRef, useMemo } from "react";
import "./ChatWindow.css";
import { getAIMessage } from "../api/api";
import { marked } from "marked";

function ChatWindow() {
  const defaultMessage = [
    { role: "assistant", content: "Hi, how can I help you today?" },
  ];

  const [messages, setMessages] = useState(defaultMessage);
  const [input, setInput] = useState("");

  // Quickstart prompt draft (fill-in form)
  const [promptDraft, setPromptDraft] = useState(null); // { id, label, template, needs: {ps, model, issue} }
  const [psValue, setPsValue] = useState("");
  const [modelValue, setModelValue] = useState("");
  const [issueValue, setIssueValue] = useState("");

  const messagesEndRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const quickPrompts = useMemo(
    () => [
      {
        id: "install",
        label: "Install a part",
        template: "How can I install part number {ps}?",
        needs: { ps: true, model: false, issue: false },
      },
      {
        id: "compat",
        label: "Check compatibility",
        template: "Is {ps} compatible with my {model} model?",
        needs: { ps: true, model: true, issue: false },
      },
      {
        id: "troubleshoot",
        label: "Troubleshoot a symptom",
        template: "My {model} has this issue: {issue}. What should I check?",
        needs: { ps: false, model: true, issue: true },
      },
      {
        id: "model_parts",
        label: "Parts for a model",
        template: "parts list for model {model}",
        needs: { ps: false, model: true, issue: false },
      },
      {
        id: "model_qna",
        label: "Model Q&A",
        template: "questions and answers for model {model}",
        needs: { ps: false, model: true, issue: false },
      },
    ],
    []
  );

  const buildFromTemplate = (template) => {
    return template
      .replaceAll("{ps}", (psValue || "").trim())
      .replaceAll("{model}", (modelValue || "").trim())
      .replaceAll("{issue}", (issueValue || "").trim());
  };

  const openPrompt = (p) => {
    // If clicking the same prompt → toggle off
    if (promptDraft && promptDraft.id === p.id) {
      setPromptDraft(null);
      setPsValue("");
      setModelValue("");
      setIssueValue("");
      return;
    }

    // Otherwise open the new prompt
    setPromptDraft(p);
    setPsValue("");
    setModelValue("");
    setIssueValue("");
  };

  const closePromptDraft = () => {
    setPromptDraft(null);
    setPsValue("");
    setModelValue("");
    setIssueValue("");
  };

  const handleSend = async (overrideText) => {
    const textToSend = (overrideText ?? input).trim();
    if (!textToSend) return;

    // add user message
    setMessages((prev) => [...prev, { role: "user", content: textToSend }]);
    setInput("");

    try {
      // get full (non-streaming) response
      const response = await getAIMessage(textToSend);

      // if your API returns extra fields (cards/sources), keep them
      setMessages((prev) => [...prev, response]);
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: "Sorry — something went wrong. Please try again.",
        },
      ]);
    }
  };

  const sendPromptDraft = () => {
    if (!promptDraft) return;

    const needPs = !!promptDraft.needs?.ps;
    const needModel = !!promptDraft.needs?.model;
    const needIssue = !!promptDraft.needs?.issue;

    if (needPs && !psValue.trim()) return;
    if (needModel && !modelValue.trim()) return;
    if (needIssue && !issueValue.trim()) return;

    const finalMsg = buildFromTemplate(promptDraft.template);
    closePromptDraft();
    setInput(finalMsg);
    setTimeout(() => handleSend(finalMsg), 0);
  };

  return (
    <div className="messages-container">
      {/* Quickstart prompts */}
      <div className="quickstart">
        <div className="quickstart-title">Quickstart</div>
        <div className="quickstart-row">
          {quickPrompts.map((p) => (
            <button
              key={p.id}
              className="quickstart-chip"
              onClick={() => openPrompt(p)}
              title={p.template}
            >
              {p.label}
            </button>
          ))}
        </div>

        {/* Inline autofill panel */}
        {promptDraft && (
          <div className="prompt-draft">
            <div className="prompt-draft-head">
              <div className="prompt-draft-title">{promptDraft.label}</div>
              <button className="prompt-draft-close" onClick={closePromptDraft}>
                ✕
              </button>
            </div>

            <div className="prompt-draft-body">
              {promptDraft.needs?.ps && (
                <div className="prompt-field">
                  <label>PS part : </label>
                  <input
                    value={psValue}
                    onChange={(e) => setPsValue(e.target.value.toUpperCase())}
                    placeholder="e.g., PS11752778"
                  />
                </div>
              )}

              {promptDraft.needs?.model && (
                <div className="prompt-field">
                  <label>Model : </label>
                  <input
                    value={modelValue}
                    onChange={(e) => setModelValue(e.target.value.toUpperCase())}
                    placeholder="e.g., WDT780SAEM1"
                  />
                </div>
              )}

              {promptDraft.needs?.issue && (
                <div className="prompt-field">
                  <label>Issue / Symptom : </label>
                  <input
                    value={issueValue}
                    onChange={(e) => setIssueValue(e.target.value)}
                    placeholder="e.g., ice maker not working, leaking water, loud noise"
                  />
                </div>
              )}

              <div className="prompt-preview">
                <span className="prompt-preview-label">Preview : </span>
                <span className="prompt-preview-text">
                  {buildFromTemplate(promptDraft.template)}
                </span>
              </div>

              <button className="prompt-send" onClick={sendPromptDraft}>
                Use this prompt
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Messages */}
      {messages.map((message, index) => (
        <div key={index} className={`${message.role}-message-container`}>
          {message.content && (
            <div className={`message ${message.role}-message`}>
              <div
                dangerouslySetInnerHTML={{
                  __html: marked(message.content).replace(/<p>|<\/p>/g, ""),
                }}
              />
            </div>
          )}
        </div>
      ))}

      <div ref={messagesEndRef} />

      <div className="input-area">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={"Type a message..."}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              handleSend();
              e.preventDefault();
            }
          }}
        />
        <button className="send-button" onClick={() => handleSend()}>
          Send
        </button>
      </div>
    </div>
  );
}

export default ChatWindow;
