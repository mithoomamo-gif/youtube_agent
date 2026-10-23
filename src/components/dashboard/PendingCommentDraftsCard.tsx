import { Link } from "react-router-dom";
import { MessageSquare } from "lucide-react";
import { StatCard } from "../ui/StatCard";
import { StatSkeleton, StatErrorCard } from "./cardStates";
import {
  isOfflineError,
  usePendingCommentDrafts,
} from "../../api/queries/dashboardGrowth";

/**
 * "Comment drafts" count card — reply drafts awaiting approval from
 * the comments pipeline (roadmap Part 7.8: draft-only classes land
 * in the Comment Inbox for a human decision).
 *
 * Data: `GET /channels/{id}/comment-drafts?status=proposed` (landed
 * comments router; pending drafts are the `proposed` ones) via
 * `usePendingCommentDrafts`, summed across connected channels.
 *
 * Click-through: the whole card links to the Comments screen
 * (`/comments`), where drafts are approved/rejected. (Approving a
 * draft there is a local decision only — posting is not wired.)
 *
 * Empty state: "—" with an explanation; never a fabricated zero when
 * the seam has not answered.
 */
export default function PendingCommentDraftsCard() {
  const drafts = usePendingCommentDrafts();
  const icon = <MessageSquare className="h-5 w-5" />;

  if (drafts.isPending) return <StatSkeleton icon={icon} />;

  if (drafts.isError) {
    return (
      <StatErrorCard
        label="Comment drafts"
        icon={icon}
        offline={isOfflineError(drafts.error)}
        onRetry={() => void drafts.refetch()}
      />
    );
  }

  const data = drafts.data;

  if (!data.hasData || data.pendingCount === null) {
    return (
      <StatCard
        label="Comment drafts"
        value="—"
        sub={
          data.endpointMissing
            ? "Comment drafts aren't available yet"
            : data.noScope
              ? "Connect a channel to start receiving drafts"
              : "No draft count reported yet"
        }
        icon={icon}
      />
    );
  }

  return (
    <Link to="/comments" className="block" aria-label="Open comment drafts">
      <StatCard
        label="Comment drafts"
        value={`${data.pendingCount}${data.countIsLowerBound ? "+" : ""}`}
        sub={
          data.pendingCount > 0
            ? "Reply drafts awaiting your approval"
            : "No drafts waiting"
        }
        icon={icon}
      />
    </Link>
  );
}
