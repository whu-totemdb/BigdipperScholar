import { FormEvent, useState } from "react";
import { BookOpen, Loader2, LogIn, UserPlus } from "lucide-react";
import type { AuthSession } from "../types";
import { loginUser, registerUser } from "../api/authHomeClient";

type LoginPageProps = {
  onAuthenticated: (session: AuthSession) => void;
};

export function LoginPage({ onAuthenticated }: LoginPageProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);

    if (mode === "register" && password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }

    setIsLoading(true);

    try {
      const session = mode === "login"
        ? await loginUser(email, password)
        : await registerUser(email, password, displayName);
      onAuthenticated(session);
    } catch (authError) {
      setError(authError instanceof Error ? authError.message : "Authentication failed");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">
          <div className="auth-mark"><BookOpen size={24} /></div>
          <div>
            <h1>BigDipper Scholar</h1>
            <p>Sign in to continue your literature graph workspaces.</p>
          </div>
        </div>

        <div className="auth-tabs" aria-label="Authentication mode">
          <button className={mode === "login" ? "active" : ""} type="button" onClick={() => setMode("login")}>
            <LogIn size={16} /> Login
          </button>
          <button className={mode === "register" ? "active" : ""} type="button" onClick={() => setMode("register")}>
            <UserPlus size={16} /> Register
          </button>
        </div>

        {mode === "register" ? (
          <label>
            Display name
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="Your name"
              required
            />
          </label>
        ) : null}

        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={mode === "register" ? "At least 8 characters" : "Password"}
            required
            minLength={mode === "register" ? 8 : 1}
          />
        </label>

        {error ? <p className="auth-error">{error}</p> : null}

        <button className="auth-submit" type="submit" disabled={isLoading}>
          {isLoading ? <Loader2 size={17} className="spin" /> : null}
          {mode === "login" ? "Login" : "Create account"}
        </button>
      </form>
    </main>
  );
}
