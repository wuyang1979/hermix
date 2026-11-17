import { useCallback, useEffect, useMemo, useState } from "react";
import { Clock, Pause, Play, Trash2 } from "lucide-react";
import { Badge, Button, H2, Spinner } from "@nous-research/ui";
import { api } from "@/lib/api";
import type { CronJob } from "@/lib/api";
import { useToast } from "@/hooks/useToast";
import { Toast } from "@/components/Toast";
import { Card, CardContent } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useI18n } from "@/i18n";
import { PluginSlot } from "@/plugins";

function formatTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

const STATUS_TONE: Record<string, "success" | "warning" | "destructive"> = {
  enabled: "success",
  scheduled: "success",
  paused: "warning",
  error: "destructive",
  completed: "destructive",
};

const PROFILE_LABELS: Record<string, string> = {};  // 开源版留空，由 config.yaml 的 display_name 决定;

// Chinese labels for job states
const STATE_LABELS: Record<string, string> = {
  enabled: "运行中",
  scheduled: "已调度",
  paused: "已暂停",
  error: "错误",
  completed: "已完成",
};

// Chinese labels for delivery targets
const DELIVERY_LABELS: Record<string, string> = {
  local: "本地存档",
  wecom: "企业微信",
  weixin: "微信",
  feishu: "飞书",
  telegram: "Telegram",
  discord: "Discord",
  slack: "Slack",
  email: "邮件",
  origin: "来源渠道",
};

function formatDeliver(deliver?: string): string {
  if (!deliver) return "—";
  // Simple values (no colon)
  if (deliver === "origin") return "📨 当前会话";
  if (deliver === "local") return "💾 本地存储";
  // platform:target format
  const col = deliver.indexOf(":");
  if (col === -1) return DELIVERY_LABELS[deliver] || deliver;
  const platform = deliver.slice(0, col);
  const target = deliver.slice(col + 1);
  let label = "";
  switch (platform) {
    case "wecom": label = "💬 企微"; break;
    case "weixin": label = "💬 微信"; break;
    case "feishu": label = "📃 飞书"; break;
    default: label = platform;
  }
  // Resolve known targets
  if (target === "WuYang" || target === "吴杨") return label + " · 吴杨";
  if (target === "Home" || target.length > 30) return label + " · 群聊";
  return label + " · " + target;
}

function ProfileGroup({
  profile,
  jobs,
  onRefresh,
}: {
  profile: string;
  jobs: CronJob[];
  onRefresh: () => void;
}) {
  const { t } = useI18n();
  const label = PROFILE_LABELS[profile] || profile;
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CronJob | null>(null);

  const doAction = async (job: CronJob, action: "pause" | "resume" | "trigger") => {
    const key = `${profile}:${job.id}:${action}`;
    setLoadingId(key);
    try {
      if (action === "pause") await api.pauseCronJob(job.id, profile);
      else if (action === "resume") await api.resumeCronJob(job.id, profile);
      else if (action === "trigger") await api.triggerCronJob(job.id, profile);
      onRefresh();
    } catch (e) {
      console.error(`${action} failed:`, e);
    } finally {
      setLoadingId(null);
    }
  };

  const doDelete = async () => {
    if (!deleteTarget) return;
    setLoadingId(`delete:${deleteTarget.id}`);
    try {
      await api.deleteCronJob(deleteTarget.id, profile);
      setDeleteTarget(null);
      onRefresh();
    } catch (e) {
      console.error("delete failed:", e);
    } finally {
      setLoadingId(null);
    }
  };

  const isEnabled = (job: CronJob) =>
    job.state === "enabled" || job.state === "scheduled";

  return (
    <div className="flex flex-col gap-2">
      <H2 variant="sm" className="flex items-center gap-2 text-muted-foreground">
        <Clock className="h-4 w-4" />
        {label}（{jobs.length} 个任务）
      </H2>
      {jobs.map((job) => {
        const enabled = isEnabled(job);
        const pauseKey = `${profile}:${job.id}:pause`;
        const resumeKey = `${profile}:${job.id}:resume`;

        return (
          <Card key={`${profile}:${job.id}`}>
            <CardContent className="flex items-center gap-4 py-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-sm truncate">
                    {job.name ||
                      job.prompt.slice(0, 60) +
                        (job.prompt.length > 60 ? "..." : "")}
                  </span>
                  <Badge tone={STATUS_TONE[job.state] ?? "secondary"}>
                    {STATE_LABELS[job.state] || job.state}
                  </Badge>
                  {job.deliver && (
                    <span className="text-xs text-muted-foreground ml-2">
                      {formatDeliver(job.deliver)}
                    </span>
                  )}
                </div>
                {job.name && (
                  <p className="text-xs text-muted-foreground truncate mb-1">
                    {job.prompt.slice(0, 100)}
                    {job.prompt.length > 100 ? "..." : ""}
                  </p>
                )}
                <div className="flex items-center gap-4 text-xs text-muted-foreground">
                  <span className="font-mono">{job.schedule_display}</span>
                  <span>
                    {t.cron.last}: {formatTime(job.last_run_at)}
                  </span>
                  <span>
                    {t.cron.next}: {formatTime(job.next_run_at)}
                  </span>
                  {job.deliver && (
                    <span>📨 {formatDeliver(job.deliver)}</span>
                  )}
                </div>
                {job.last_error && (
                  <p className="text-xs text-destructive mt-1">
                    {job.last_error}
                  </p>
                )}
              </div>

              {/* Action buttons: 停止/启动 + 删除 */}
              <div className="flex items-center gap-2 shrink-0">
                {enabled ? (
                  <Button
                    ghost
                    size="sm"
                    disabled={loadingId === pauseKey}
                    onClick={() => doAction(job, "pause")}
                    prefix={<Pause className="h-4 w-4" />}
                    className="h-9"
                    title="暂停"
                  >
                    暂停
                  </Button>
                ) : (
                  <Button
                    ghost
                    size="sm"
                    disabled={loadingId === resumeKey}
                    onClick={() => doAction(job, "resume")}
                    prefix={<Play className="h-4 w-4" />}
                    className="h-9"
                    title="启动"
                  >
                    启动
                  </Button>
                )}

                <Button
                  outlined
                  size="sm"
                  destructive
                  disabled={loadingId === `delete:${job.id}`}
                  onClick={() => setDeleteTarget(job)}
                  prefix={<Trash2 className="h-4 w-4" />}
                  className="h-9"
                  title="删除"
                >
                  删除
                </Button>
              </div>
            </CardContent>
          </Card>
        );
      })}

      <ConfirmDialog
        open={!!deleteTarget}
        title={t.cron.confirmDeleteTitle}
        description={t.cron.confirmDeleteMessage}
        confirmLabel={t.common.delete}
        destructive
        loading={!!loadingId?.startsWith("delete:")}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={doDelete}
      />
    </div>
  );
}

export default function CronPage() {
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { t } = useI18n();

  const loadJobs = useCallback(() => {
    setLoading(true);
    api
      .getAllProfileCronJobs()
      .then(setJobs)
      .catch(() => showToast(t.common.loading, "error"))
      .finally(() => setLoading(false));
  }, [showToast, t.common.loading]);

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  // Group jobs by profile
  const groupedJobs = useMemo(() => {
    const groups = new Map<string, CronJob[]>();
    for (const job of jobs) {
      const profile = job.profile || "default";
      if (!groups.has(profile)) groups.set(profile, []);
      groups.get(profile)!.push(job);
    }
    return groups;
  }, [jobs]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  // Define profile display order for grouped view
  const profileOrder = ["default", "xiaoyi", "xiaozhua", "xiaojiu", "sales-advisor", "intel-sentinel", "fullstack-bot", "main-bot"];
  const sortedGroups = Array.from(groupedJobs.entries()).sort(([a], [b]) => {
    const ai = profileOrder.indexOf(a);
    const bi = profileOrder.indexOf(b);
    return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
  });

  return (
    <div className="flex flex-col gap-6">
      <PluginSlot name="cron:top" />
      <Toast toast={toast} />

      {sortedGroups.length === 0 && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            {t.cron.noJobs}
          </CardContent>
        </Card>
      )}

      {sortedGroups.map(([profile, profileJobs]) => (
        <ProfileGroup
          key={profile}
          profile={profile}
          jobs={profileJobs}
          onRefresh={loadJobs}
        />
      ))}

      <PluginSlot name="cron:bottom" />
    </div>
  );
}
