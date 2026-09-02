# Automatic Face Attendance System — Pure Python + MongoDB

A small-school attendance system for one class of around 40 students. It uses a laptop camera and browser-local face matching instead of QR codes, QR passes, or QR scanning.

This source uses:

- **Python standard-library WSGI** for the web application
- **MongoDB Atlas** for permanent storage
- **PyMongo** as the official MongoDB Python driver
- **No Flask, no SQLite, no Turso, and no QR package**

The `app` object in `app.py` is a standard WSGI callable that Vercel's Python runtime can run directly.

## Features

- Student registration with manual details and **one clear, front-facing face capture**.
- A single visible upward arrow and English enrollment instructions; no left/right or three-capture flow.
- MongoDB storage for student data, face descriptors, face-profile images, attendance captures, and settings.
- Automatic browser-local face matching using locally served face-api models.
- Automatic display of the matched student's real name, gender, grade, and section.
- Automatic daily Time In:
  - at or before **8:00 AM**: **Present**
  - after **8:00 AM**: **Late**
- Automatic same-day face-based Check Out from **5:00 PM**, linked to the Time In record.
- Duplicate Time In and Check Out prevention.
- Attendance filters and CSV export for Excel.
- HTTP Basic Authentication for Vercel deployments.

> Face matching is not liveness or anti-spoofing detection. Obtain appropriate consent, provide a manual alternative, and do not use an automatic match as the only basis for a high-impact decision.

---

## Requirements

- Python 3.10 or newer
- Google Chrome or Microsoft Edge
- A working laptop webcam
- MongoDB Atlas cluster and database user
- Vercel account for cloud deployment

`requirements.txt` contains only the MongoDB connection dependencies:

```text
pymongo==4.10.1
dnspython==2.7.0
```

---

## Project structure

```text
app.py                  Pure-Python WSGI app and Vercel Function entrypoint
public/static/          Browser face models, JavaScript, and CSS
requirements.txt        PyMongo and DNS dependencies
.env.example            Safe environment-variable example; no secrets
vercel.json             Vercel Function configuration
```

There is no `templates/` folder because HTML pages are securely rendered by `app.py` using Python's standard library.

MongoDB stores compact, validated profile and attendance images inside its documents. The Vercel filesystem is never used for permanent student data.

---

# Local laptop setup

## 1. Create `.env`

Copy `.env.example` to a new file named `.env` in the folder beside `app.py`.

Set:

```env
MONGODB_URI=mongodb+srv://YOUR_DATABASE_USER:YOUR_URL_ENCODED_PASSWORD@YOUR_CLUSTER.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=face-scan
SECRET_KEY=your-private-local-random-secret
```

The application reads `.env` locally using Python's standard library. `.env` is excluded from Git and must never be uploaded to GitHub.

## 2. Start the app

Double-click `START_WINDOWS.bat`, or run:

```bash
cd school-attendance
python -m pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

Allow the camera in Chrome or Edge.

---

# MongoDB Atlas setup

## 1. Start a cluster

In MongoDB Atlas, create or select an active cluster.

## 2. Create a dedicated database user

Create a private database user for this attendance system. Grant it the built-in role:

```text
readWrite
```

for this database:

```text
face-scan
```

Use a strong password. If the password has reserved URI characters such as `@`, `:`, `/`, `?`, `#`, `[`, or `]`, URL-encode it before adding it to the connection string.

## 3. Configure Atlas Network Access

Vercel Functions need permission to reach MongoDB Atlas. In Atlas, open:

```text
Security → Network Access → Add IP Address
```

For a basic Vercel deployment, select **Allow Access from Anywhere**, which creates:

```text
0.0.0.0/0
```

Vercel serverless functions do not normally use one fixed outgoing IP address. For a higher-security production setup, use restricted egress or private connectivity where available.

## 4. Copy the Python driver URI

In Atlas, select:

```text
Connect → Drivers → Python
```

Copy the connection string. It should follow this safe pattern:

```text
mongodb+srv://YOUR_DATABASE_USER:YOUR_URL_ENCODED_PASSWORD@YOUR_CLUSTER.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
```

Important:

- Use the actual MongoDB database-user name and password.
- Do not include square brackets around the password.
- Do not include quote marks.
- Do not use `mailto:`.
- Use a real `&`, never the HTML text `&amp;`.
- Do not save the URI in source code, GitHub, screenshots, or chat.

The application uses the name set in `MONGODB_DB`. Set it to:

```text
face-scan
```

The app creates and maintains these collections and indexes automatically:

```text
students
attendance
settings
```

---

# Deploy on Vercel

## 1. Put the source in GitHub

Push the **contents** of the `school-attendance` folder into the root of your GitHub repository.

The repository root must contain:

```text
app.py
requirements.txt
public/
vercel.json
```

Do not upload only the ZIP file. Do not keep the actual source inside an extra nested folder unless you configure that folder as Vercel's Root Directory.

## 2. Add Vercel Environment Variables

Open:

```text
Vercel → Project → Settings → Environment Variables
```

Add all five as **Secret** values for **Production and Preview**:

| Variable | Required value |
|---|---|
| `MONGODB_URI` | Exact MongoDB Atlas driver URI with current credentials |
| `MONGODB_DB` | `face-scan` |
| `SECRET_KEY` | New long random private value |
| `APP_ACCESS_USERNAME` | Private app username, for example `teacher` |
| `APP_ACCESS_PASSWORD` | Strong private browser-login password |

Generate a new private `SECRET_KEY` if needed:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do not reuse an API key, a MongoDB password, or the database URI as `SECRET_KEY` or `APP_ACCESS_PASSWORD`.

## 3. Deploy

In Vercel:

- **Framework Preset:** choose **Other**.
- **Root Directory:** `./` if `app.py` is at the GitHub repository root.
- **Build Command:** leave the default.
- **Output Directory:** leave blank.

Deploy or redeploy after all five variables are saved.

## 4. Open the production app

Use the newest Production deployment's **Visit** button. The browser must first show a Basic Authentication prompt:

```text
Username: APP_ACCESS_USERNAME
Password: APP_ACCESS_PASSWORD
```

After a successful sign-in, the Python app connects to MongoDB, creates its required indexes, and displays the dashboard.

### Deployment diagnostics

| Screen | Meaning |
|---|---|
| **Cloud setup required** | A required Vercel variable is missing or invalid. Add the named variable and redeploy. |
| **Cloud database unavailable** | Vercel received the variables but cannot reach Atlas. Check cluster status, Network Access, database-user permissions, password, URI, and database name. |
| Browser login prompt | Configuration was received. Enter the app username and password. |

---

# Use the system

## Register a student

1. Open **Students**.
2. Select **Register student**.
3. Enter the required student details.
4. Select **Start face capture** and allow camera access.
5. Keep only one face in the frame and ask the student to look straight ahead.
6. Select **Capture face** once.
7. Select **Register face profile**.

## Automatic Time In

1. Open **Face Attendance** and select **Time In**.
2. Start the face scanner.
3. Keep one student in the camera frame.
4. Wait for a stable automatic match.

The app shows the student's name, gender, grade, and section, then saves Time In automatically as Present or Late.

## Automatic Check Out

1. Select **Check Out** in Face Attendance.
2. Scan one student at a time at or after the configured Check Out time.
3. Check Out is saved only for a student who already has a same-day Time In.

## Records and export

Open **Records** to filter by date, name, student number, grade, section, or attendance status. Select **Download CSV** to export results for Excel.

---

# Privacy and readiness

1. Obtain informed consent from students and, where appropriate, parents or guardians.
2. Keep the MongoDB Atlas account, Vercel account, app username/password, and authorized devices private.
3. Rotate secrets immediately if they were exposed in chat, screenshots, GitHub, or another public location.
4. Test the real Atlas connection, Basic Authentication, browser camera permission, lighting, enrollment, matching, Time In cutoff, Check Out cutoff, duplicate prevention, and CSV export before classroom use.
5. Keep an alternative/manual attendance method available.

## Technical notes

- `app.py` is a standard WSGI application and has no Flask dependency.
- `public/static/vendor/face-api.min.js` and `public/static/models/` keep face matching in the browser.
- The Python server validates the descriptor match again before saving attendance in MongoDB.
- Images are capped at 1 MB decoded size to remain under MongoDB document limits and Vercel Function request limits.
- No QR scanner, QR package, server-side OpenCV, dlib, Python `face_recognition`, or cloud face-recognition API is used.
- See `THIRD_PARTY_NOTICES.md` and `public/static/vendor/face-api-LICENSE.txt` for third-party attribution.
