export type Retention = { archive_after_days: number; delete_after_days: number };

export type RecordingItem = {
  id: string;
  username: string | null;
  node_name: string | null;
  target: string | null;
  remote_user: string | null;
  started_at: string;
  ended_at: string | null;
  bytes_out: number;
  archived_at: string;
  keep: boolean;
  recorded: boolean;
  expires_at: string | null;
};

export type ChatItem = {
  id: string;
  title: string;
  node_names: string[];
  created_by: string;
  created_at: string;
  updated_at: string;
  archived_at: string;
  keep: boolean;
  messages: number;
  expires_at: string | null;
};

export type ArchivePage<T> = { items: T[]; total: number; searched_content: boolean };

export function whenExpires(item: { keep: boolean; expires_at: string | null }): string {
  if (item.keep) return "kept";
  if (!item.expires_at) return "—";
  const days = Math.ceil((new Date(item.expires_at).getTime() - Date.now()) / 86400000);
  return days <= 0 ? "today" : days === 1 ? "in 1 day" : `in ${days} days`;
}
