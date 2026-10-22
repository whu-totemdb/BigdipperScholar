import { Bell, BookOpen, Compass, Home, Search, UserRound } from "lucide-react";

type TopNavProps = {
  displayName: string;
  onHome: () => void;
};

export function TopNav({ displayName, onHome }: TopNavProps) {
  return (
    <header className="top-nav">
      <div className="brand-group">
        <div className="brand-mark">
          <Compass size={20} strokeWidth={2.6} />
        </div>
        <div className="brand-name">BigDipper Scholar</div>
      </div>

      <nav className="main-tabs" aria-label="Primary">
        <button className="tab-button active" type="button" onClick={onHome}>
          <Home size={16} />
          Home
        </button>
        <button className="tab-button" type="button">
          <Search size={16} />
          Search
        </button>
        <button className="tab-button" type="button">
          <BookOpen size={16} />
          Library
        </button>
      </nav>

      <div className="account-actions">
        <button className="icon-button" type="button" aria-label="Notifications">
          <Bell size={17} />
        </button>
        <button className="user-chip" type="button">
          <UserRound size={16} />
          {displayName}
        </button>
      </div>
    </header>
  );
}
