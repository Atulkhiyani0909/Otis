
export const success_response = `
<!DOCTYPE html>
  <html lang="en">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Authentication Successful</title>
    <style>
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background-color: #f4f7f9;
        color: #24292e;
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100vh;
        margin: 0;
      }
      .card {
        background: #ffffff;
        padding: 40px 32px;
        border-radius: 16px;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.05);
        text-align: center;
        max-width: 400px;
        width: 90%;
      }
      .icon-container {
        width: 72px;
        height: 72px;
        background: #e6f6ec;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 0 auto 24px;
      }
      .icon {
        width: 36px;
        height: 36px;
        color: #22c55e;
      }
      h2 {
        font-size: 24px;
        margin: 0 0 12px;
        color: #1a1a1a;
      }
      p {
        font-size: 15px;
        line-height: 1.6;
        color: #626d7a;
        margin: 0 0 24px;
      }
      .badge {
        display: inline-block;
        background: #f0fdf4;
        color: #166534;
        font-size: 13px;
        font-weight: 600;
        padding: 6px 16px;
        border-radius: 99px;
        border: 1px solid #bbf7d0;
        margin-bottom: 24px;
      }
      .action-hint {
        font-size: 13px;
        color: #8a94a6;
        border-top: 1px solid #edf2f7;
        padding-top: 20px;
        margin: 0;
      }
    </style>
  </head>
  <body>
    <div class="card">
      <div class="icon-container">
        <!-- SVG Checkmark Icon -->
        <svg class="icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://w3.org">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"></path>
        </svg>
      </div>
      <h2>Connected to Otis</h2>
      <div class="badge">Google Account Linked</div>
      <p>Your Google credentials have been securely stored. Otis is now ready to assist you with your synced data.</p>
      <p class="action-hint">You can safely close this tab and return to your Telegram chat.</p>
    </div>
  </body>
  </html>
`