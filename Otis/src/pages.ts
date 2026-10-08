// pages.ts
// HTML for the Home, Privacy Policy and Terms of Service pages.
// Import these in your server and send them with app.get (see routes example).

export const CONFIG = {
  appName: "Otis Assistant", // must match the app name on your Google OAuth consent screen exactly
  botUsername: "OtisExecutiveBot",
  ownerName: "Atul Khiyani",
  contactEmail: "atulkhiyani09@gmail.com",
  domain: "https://otis-1-keq6.onrender.com",
  lastUpdated: "October 9, 2026",
  country: "India",
  // Paste the token from Google Search Console (HTML tag method) into the
  // GOOGLE_SITE_VERIFICATION env var, or here.
  googleSiteVerification: process.env.GOOGLE_SITE_VERIFICATION || "",
  // List the Google data your app really accesses. Google compares this
  // against the OAuth scopes you request, so keep them in sync.
  googleData: [
    "Google Calendar: view, create and edit events so Otis Assistant can manage your schedule",
    "Gmail: read emails and send or draft emails on your instruction",
    "Google Drive: find, read and create files on your instruction",
    "Google Sheets: read and update spreadsheets on your instruction",
    "Google Contacts: look up contact names, emails and phone numbers when you ask Otis Assistant to contact someone",
    "Google Tasks: view, create and complete tasks on your instruction",
    "Basic profile: your name and email address, used to identify your account",
  ],
};

const TELEGRAM_URL: string = "https://t.me/" + CONFIG.botUsername;

// ---------------------------------------------------------------------------
// Shared layout
// ---------------------------------------------------------------------------
const css = `
  :root {
    --bg: #f2f5f8;
    --panel: #ffffff;
    --ink: #13202c;
    --muted: #5a6b7a;
    --line: #d9e1e8;
    --accent: #1f6fb2;
    --accent-dark: #17598f;
    --deep: #0f2233;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1a26;
      --panel: #132536;
      --ink: #e8eef4;
      --muted: #9db0c0;
      --line: #223a50;
      --accent: #5eaee8;
      --accent-dark: #86c2f0;
      --deep: #08121b;
    }
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.65;
  }
  a { color: var(--accent); }
  a:focus-visible, button:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }
  header.site {
    border-bottom: 1px solid var(--line);
    background: var(--panel);
  }
  .bar {
    max-width: 960px;
    margin: 0 auto;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .brand {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--ink);
    text-decoration: none;
    letter-spacing: 0.01em;
  }
  nav a {
    margin-left: 20px;
    color: var(--muted);
    text-decoration: none;
    font-size: 0.95rem;
  }
  nav a:hover { color: var(--accent); text-decoration: underline; }
  main { max-width: 960px; margin: 0 auto; padding: 48px 24px 64px; }

  /* Home */
  .hero { padding: 24px 0 8px; max-width: 680px; }
  .hero h1 {
    font-family: Georgia, "Times New Roman", serif;
    font-size: clamp(2.2rem, 6vw, 3.6rem);
    line-height: 1.1;
    margin: 0 0 20px;
  }
  .hero p { font-size: 1.15rem; color: var(--muted); margin: 0 0 28px; }
  .btn {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    padding: 14px 26px;
    border-radius: 8px;
    font-weight: 600;
    text-decoration: none;
    font-size: 1.05rem;
  }
  .btn:hover { background: var(--accent-dark); }
  .handle { margin-top: 14px; color: var(--muted); font-size: 0.95rem; }

  .chat {
    margin: 48px 0 8px;
    background: var(--deep);
    border-radius: 12px;
    padding: 24px;
    max-width: 560px;
  }
  .msg { padding: 10px 14px; border-radius: 12px; margin: 0 0 10px; max-width: 85%; font-size: 0.97rem; }
  .msg.you { background: #2b5278; color: #fff; margin-left: auto; }
  .msg.bot { background: #1b2f42; color: #e8eef4; }
  .chat .msg:last-child { margin-bottom: 0; }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 28px;
    margin-top: 56px;
    border-top: 1px solid var(--line);
    padding-top: 32px;
  }
  .grid h3 { margin: 0 0 6px; font-size: 1.1rem; }
  .grid p { margin: 0; color: var(--muted); }

  /* Legal pages */
  article {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 40px clamp(20px, 5vw, 56px);
    max-width: 780px;
  }
  article h1 {
    font-family: Georgia, "Times New Roman", serif;
    font-size: clamp(1.8rem, 4vw, 2.4rem);
    margin: 0 0 6px;
  }
  article h2 { font-size: 1.25rem; margin: 36px 0 8px; }
  article p, article li { color: var(--ink); }
  article ul { padding-left: 22px; }
  article li { margin-bottom: 6px; }
  .meta { color: var(--muted); margin: 0 0 24px; }
  .note {
    border-left: 4px solid var(--accent);
    background: var(--bg);
    padding: 14px 18px;
    border-radius: 0 8px 8px 0;
    margin: 20px 0;
  }

  footer.site {
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 0.9rem;
  }
  footer.site .bar { padding: 24px; }
  footer.site a { margin-right: 18px; }
`;

export function layout(title: string, description: string, body: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${title}</title>
  <meta name="description" content="${description}">
  ${CONFIG.googleSiteVerification ? `<meta name="google-site-verification" content="${CONFIG.googleSiteVerification}">` : ""}
  <style>${css}</style>
</head>
<body>
  <header class="site">
    <div class="bar">
      <a class="brand" href="/">${CONFIG.appName}</a>
      <nav>
        <a href="/privacy-policy">Privacy Policy</a>
        <a href="/terms">Terms of Service</a>
      </nav>
    </div>
  </header>
  <main>
    ${body}
  </main>
  <footer class="site">
    <div class="bar">
      <span>&copy; ${new Date().getFullYear()} ${CONFIG.appName}</span>
      <span>
        <a href="/privacy-policy">Privacy Policy</a>
        <a href="/terms">Terms of Service</a>
        <a href="mailto:${CONFIG.contactEmail}">Contact</a>
      </span>
    </div>
  </footer>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// Page bodies
// ---------------------------------------------------------------------------
export function homeBody(): string {
  return `
    <section class="hero">
      <h1>${CONFIG.appName} handles the admin so you can get on with the work.</h1>
      <p>
        ${CONFIG.appName} is a personal executive assistant that lives in Telegram.
        Message it in plain language to manage your calendar, email, files, spreadsheets,
        contacts and tasks.
      </p>
      <a class="btn" href="${TELEGRAM_URL}" target="_blank" rel="noopener noreferrer">
        Open ${CONFIG.appName} in Telegram
      </a>
      <div class="handle">Bot: @${CONFIG.botUsername}</div>
    </section>

    <section class="chat" aria-label="Example conversation">
      <p class="msg you">What's on my calendar tomorrow?</p>
      <p class="msg bot">You have two meetings: 10:00 design review and 15:30 call with the vendor.</p>
      <p class="msg you">Move the vendor call to Thursday.</p>
      <p class="msg bot">Done. The vendor call is now on Thursday at 15:30.</p>
    </section>

    <section class="grid">
      <div>
        <h3>Works where you already chat</h3>
        <p>No new app to install. Open the bot in Telegram and start typing.</p>
      </div>
      <div>
        <h3>You choose what it can access</h3>
        <p>Google access is optional, asked for with a standard Google sign-in screen, and you can revoke it at any time.</p>
      </div>
      <div>
        <h3>Your data stays yours</h3>
        <p>
          We never sell your data. Read the <a href="/privacy-policy">Privacy Policy</a>
          and <a href="/terms">Terms of Service</a>.
        </p>
      </div>
    </section>
  `;
}

export function privacyBody(): string {
  const googleList = CONFIG.googleData.map((d) => `<li>${d}</li>`).join("\n");
  return `
  <article>
    <h1>Privacy Policy</h1>
    <p class="meta">Last updated: ${CONFIG.lastUpdated}</p>

    <p>
      This Privacy Policy explains how ${CONFIG.appName} ("we", "us", "the Service"),
      operated by ${CONFIG.ownerName}, collects, uses and protects your information when
      you use the Telegram bot @${CONFIG.botUsername} and this website (${CONFIG.domain}).
    </p>

    <h2>1. Information we collect</h2>
    <ul>
      <li><strong>Telegram information:</strong> your Telegram user ID, username, first name and the messages you send to the bot.</li>
      <li><strong>Account and settings data:</strong> preferences and settings you configure in the bot.</li>
      <li><strong>Google user data (only if you connect your Google account):</strong> data from the Google services you authorize, as described in section 2.</li>
      <li><strong>Technical data:</strong> basic logs such as timestamps and error messages, used to keep the Service running.</li>
    </ul>

    <h2>2. Google user data</h2>
    <p>If you choose to connect your Google account, ${CONFIG.appName} requests access only to the following:</p>
    <ul>
      ${googleList}
    </ul>
    <p>We use this data only to perform the actions you ask for inside the bot, such as showing your events or sending a message you requested. We do not access Google data in the background for any other purpose.</p>

    <div class="note">
      <strong>Google API Services User Data Policy.</strong>
      ${CONFIG.appName}'s use and transfer to any other app of information received from
      Google APIs will adhere to the
      <a href="https://developers.google.com/terms/api-services-user-data-policy" target="_blank" rel="noopener noreferrer">Google API Services User Data Policy</a>,
      including the Limited Use requirements.
    </div>

    <p>In line with the Limited Use requirements:</p>
    <ul>
      <li>We use Google user data only to provide and improve the user-facing features you request.</li>
      <li>We do not transfer Google user data to others except as needed to provide the Service, to comply with law, or as part of a merger or sale with notice to you.</li>
      <li>We do not use Google user data for advertising, including retargeting or interest-based ads.</li>
      <li>We do not allow humans to read your Google user data unless you give consent for specific items, it is needed for security or to comply with law, or the data is aggregated and anonymized for internal operations.</li>
      <li>We do not use Google user data to develop, improve or train generalized artificial intelligence or machine learning models.</li>
    </ul>

    <h2>3. How we use your information</h2>
    <ul>
      <li>To respond to your messages and carry out your requests.</li>
      <li>To authenticate you and keep your connected accounts working.</li>
      <li>To fix bugs, prevent abuse and maintain security.</li>
      <li>To contact you about important changes to the Service.</li>
    </ul>

    <h2>4. Sharing of information</h2>
    <p>We do not sell or rent your personal information. We share information only with service providers needed to run the Service (for example, hosting, database and AI processing providers), and only to the extent required to provide the features you use. These providers act on our instructions and may not use your data for their own purposes. We may also disclose information if required by law.</p>

    <h2>5. Data storage and security</h2>
    <p>Data is stored on secured servers. Access tokens and other credentials are stored encrypted, and access is limited to what is needed to operate the Service. All data in transit uses HTTPS/TLS. No system is perfectly secure, but we work to protect your information.</p>

    <h2>6. Data retention and deletion</h2>
    <p>We keep your data only as long as your account is active or as needed to provide the Service. You can:</p>
    <ul>
      <li>Disconnect Google at any time from your Google Account at <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener noreferrer">myaccount.google.com/permissions</a>. We will stop accessing your Google data immediately.</li>
      <li>Request deletion of all data we hold about you by emailing <a href="mailto:${CONFIG.contactEmail}">${CONFIG.contactEmail}</a> from the address linked to your account or by messaging the bot. We will delete your data within 30 days.</li>
    </ul>

    <h2>7. Your rights</h2>
    <p>Depending on where you live, you may have the right to access, correct, export or delete your personal data, or to object to how it is processed. Contact us using the details below to exercise these rights.</p>

    <h2>8. Telegram</h2>
    <p>Your use of Telegram is governed by Telegram's own terms and privacy policy. We receive only the information that Telegram provides to bots.</p>

    <h2>9. Children's privacy</h2>
    <p>The Service is not intended for children under 13 (or the minimum age in your country). We do not knowingly collect their data. If you believe a child has used the Service, contact us and we will delete the data.</p>

    <h2>10. Changes to this policy</h2>
    <p>We may update this policy from time to time. The "Last updated" date above shows the latest version. If changes are significant, we will notify you through the bot or on this website.</p>

    <h2>11. Contact</h2>
    <p>
      Questions or requests: <a href="mailto:${CONFIG.contactEmail}">${CONFIG.contactEmail}</a><br>
      ${CONFIG.ownerName}, ${CONFIG.country}
    </p>
  </article>
  `;
}

export function termsBody(): string {
  return `
  <article>
    <h1>Terms of Service</h1>
    <p class="meta">Last updated: ${CONFIG.lastUpdated}</p>

    <p>
      These Terms of Service ("Terms") govern your use of ${CONFIG.appName}, including the
      Telegram bot @${CONFIG.botUsername} and the website at ${CONFIG.domain} (together, the "Service"),
      operated by ${CONFIG.ownerName}. By using the Service you agree to these Terms. If you do not agree, please do not use it.
    </p>

    <h2>1. The Service</h2>
    <p>${CONFIG.appName} is a personal assistant bot that helps you manage tasks such as scheduling and communication through Telegram, and, if you choose to connect them, third-party services such as Google. Features may change, be added or be removed at any time.</p>

    <h2>2. Eligibility and accounts</h2>
    <p>You must be old enough to use Telegram in your country and able to form a binding agreement. You are responsible for activity under your Telegram account and any accounts you connect to the Service.</p>

    <h2>3. Connecting third-party accounts</h2>
    <p>If you connect a Google account, you authorize ${CONFIG.appName} to access the data and take the actions described in our <a href="/privacy-policy">Privacy Policy</a>, on your instruction. You can revoke access at any time in your Google Account settings. Your use of Google services remains subject to Google's terms.</p>

    <h2>4. Acceptable use</h2>
    <p>You agree not to:</p>
    <ul>
      <li>Use the Service for anything unlawful, harmful, harassing or fraudulent.</li>
      <li>Send spam or unsolicited bulk messages through the Service.</li>
      <li>Attempt to disrupt, reverse engineer, overload or gain unauthorized access to the Service or its systems.</li>
      <li>Use the Service to access accounts or data you do not have permission to use.</li>
    </ul>

    <h2>5. AI-generated content</h2>
    <p>The Service may use artificial intelligence to understand your requests and draft content. AI output can be incorrect or incomplete. You are responsible for reviewing it, especially before relying on it or before it is sent to others on your behalf.</p>

    <h2>6. Privacy</h2>
    <p>Our <a href="/privacy-policy">Privacy Policy</a> explains how we handle your data and forms part of these Terms.</p>

    <h2>7. Availability</h2>
    <p>We aim to keep the Service running but do not guarantee uninterrupted or error-free operation. We may suspend or discontinue the Service, or restrict your access, at any time, including if you breach these Terms.</p>

    <h2>8. Intellectual property</h2>
    <p>The Service, its code and its branding belong to ${CONFIG.ownerName}. You keep ownership of the content you send to the Service and give us a limited licence to process it solely to provide the Service to you.</p>

    <h2>9. Disclaimer of warranties</h2>
    <p>The Service is provided "as is" and "as available", without warranties of any kind, whether express or implied, including fitness for a particular purpose and non-infringement.</p>

    <h2>10. Limitation of liability</h2>
    <p>To the maximum extent permitted by law, ${CONFIG.ownerName} will not be liable for any indirect, incidental, special or consequential damages, or for loss of data, profits or business, arising from your use of the Service. Our total liability for any claim is limited to the amount you paid for the Service in the previous 12 months, or zero if the Service is free.</p>

    <h2>11. Termination</h2>
    <p>You may stop using the Service at any time and may ask us to delete your data as described in the Privacy Policy. We may terminate or suspend access if you violate these Terms.</p>

    <h2>12. Changes to these Terms</h2>
    <p>We may update these Terms from time to time. Continued use of the Service after changes take effect means you accept the updated Terms.</p>

    <h2>13. Governing law</h2>
    <p>These Terms are governed by the laws of ${CONFIG.country}, without regard to conflict-of-law rules.</p>

    <h2>14. Contact</h2>
    <p>Questions about these Terms: <a href="mailto:${CONFIG.contactEmail}">${CONFIG.contactEmail}</a></p>
  </article>
  `;
}

