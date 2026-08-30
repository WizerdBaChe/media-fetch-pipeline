import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./theme.css";

const container = document.getElementById("root");
if (!container) {
  // A blank page with no explanation is a defect. Say why.
  document.body.textContent = "找不到 #root 容器，無法啟動介面。";
} else {
  createRoot(container).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
