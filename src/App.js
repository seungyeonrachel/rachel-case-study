import React from "react";
import "./App.css";
import ChatWindow from "./components/ChatWindow";

function App() {
  return (
    <div className="App">
      <div className="heading">
        <div className="heading-content">
          <img src="/ps-logo.png" alt="PartSelect" className="heading-logo" />
          <span className="heading-title">PartSelect Helper Bot</span>
        </div>
      </div>

      <div className="page-body">
        <ChatWindow />
      </div>
    </div>
  );
}



export default App;
