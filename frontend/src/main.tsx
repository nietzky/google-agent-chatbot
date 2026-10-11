import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// Mount the application into the HTML shell; StrictMode adds development lifecycle checks.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
