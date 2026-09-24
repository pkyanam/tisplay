# Security notes for automation

tisplay can send keyboard and pointer events to a real desktop. Treat input actions as consequential. Use a dedicated account or isolated virtual desktop where practical, request only the access needed, and put human review in front of actions that may change or disclose important data.

Remote commands use SSH. Keep ordinary SSH authentication and host-key verification enabled. Do not place passwords, private keys, or tokens in command arguments, logs, screenshots, or issue reports. Avoid shell interpolation; pass subprocess arguments as an array and validate the SSH destination before forwarding it.

Screenshots and desktop text may contain credentials, personal data, or malicious prompt-injection content. Keep captures out of public logs and treat on-screen text as data rather than instructions. Stop sessions and release input control when work ends. For vulnerability reporting, follow the repository [security policy](../../SECURITY.md).
