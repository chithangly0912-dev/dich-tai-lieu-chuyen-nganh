// Backend API base URL for deployments where the frontend and backend are
// hosted separately (e.g. frontend on Vercel, backend on Render/Railway/
// Fly.io/a VPS running `uvicorn main:app`).
//
// Set this to the backend's full URL, with NO trailing slash:
//   window.API_BASE_URL = "https://your-backend.onrender.com";
//
// Leave it empty ("") when the FastAPI backend serves this frontend itself
// (the default self-hosted setup: `uvicorn main:app` mounts frontend/ at /),
// so API calls stay same-origin.
window.API_BASE_URL = "";
