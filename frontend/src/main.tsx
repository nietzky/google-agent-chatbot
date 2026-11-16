import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Notifications from "./Notifications";
import AppCenter from "./AppCenter";
import "./styles.css";

// Mount the application into the HTML shell; StrictMode adds development lifecycle checks.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {window.location.pathname === "/app-center" ? <AppCenter /> : window.location.pathname === "/notifications" ? <Notifications /> : <App />}
  </React.StrictMode>,
);
