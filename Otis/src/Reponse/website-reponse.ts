const BOT_USERNAME = "OtisExecutiveBot";
const TELEGRAM_URL = `https://t.me/${BOT_USERNAME}`;
const REDIRECT_SECONDS = 5;

export const success_response = `
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Authentication Successful - Otis Assistant</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: #f4f7f9;
      color: #24292e;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
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
    .icon { width: 36px; height: 36px; color: #22c55e; }
    h2 { font-size: 24px; margin: 0 0 12px; color: #1a1a1a; }
    p { font-size: 15px; line-height: 1.6; color: #626d7a; margin: 0 0 24px; }
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
    .btn {
      display: block;
      background: #229ed9;
      color: #ffffff;
      text-decoration: none;
      font-weight: 600;
      font-size: 16px;
      padding: 14px 24px;
      border-radius: 10px;
      margin-bottom: 16px;
    }
    .btn:hover { background: #1b87bb; }
    .btn:focus-visible { outline: 3px solid #1b87bb; outline-offset: 3px; }
    .timer { font-size: 14px; color: #626d7a; margin: 0 0 20px; }
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
      <svg class="icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"></path>
      </svg>
    </div>
    <h2>Connected to Otis Assistant</h2>
    <div class="badge">Google Account Linked</div>
    <p>Your Google credentials have been securely stored. Otis Assistant is now ready to help you with your synced data.</p>

    <a class="btn" id="open-bot" href="${TELEGRAM_URL}">Back to Telegram</a>
    <p class="timer" id="timer">Returning to Telegram in <strong id="count">${REDIRECT_SECONDS}</strong>s</p>

    <p class="action-hint">If nothing happens, tap the button above or close this tab and return to your Telegram chat.</p>
  </div>

  <script>
    (function () {
      var seconds = ${REDIRECT_SECONDS};
      var url = "${TELEGRAM_URL}";
      var countEl = document.getElementById("count");
      var timerEl = document.getElementById("timer");

      var interval = setInterval(function () {
        seconds -= 1;
        if (seconds <= 0) {
          clearInterval(interval);
          timerEl.textContent = "Opening Telegram...";
          window.location.href = url;
          return;
        }
        countEl.textContent = seconds;
      }, 1000);

      // If the user taps the button, stop the countdown so it doesn't fire twice.
      document.getElementById("open-bot").addEventListener("click", function () {
        clearInterval(interval);
      });
    })();
  </script>
</body>
</html>
`;