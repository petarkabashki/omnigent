import { useCallback, useMemo, useState } from "react";
import type { Node, NodeProps, NodeTypes } from "@xyflow/react";
import { Background, ReactFlow, ReactFlowProvider } from "@xyflow/react";
import { useCanvasSessions } from "@/canvas/canvasSessions";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useProjects } from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { useNavigate } from "@/lib/routing";
import { projectPathOf } from "@/lib/projectTree";
import { buildSessionGraph } from "./sessionGraph";
import { AgentNodeComponent, ZoomControls } from "./SubagentsGraphView";

import "@xyflow/react/dist/style.css";

function HeadingNode({ data }: NodeProps<Node<{ label: string }>>) {
  return <div className="text-sm font-medium text-muted-foreground">{data.label}</div>;
}

const nodeTypes: NodeTypes = { agent: AgentNodeComponent, heading: HeadingNode };

interface SessionGraphDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  activeSessionId: string | null;
}

/** Every session, which session started it, and where it runs. */
export function SessionGraphDialog({
  open,
  onOpenChange,
  activeSessionId,
}: SessionGraphDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="session-graph-dialog"
        className="flex h-[85vh] max-w-[90vw] flex-col gap-3 sm:max-w-[90vw]"
      >
        <DialogHeader>
          <DialogTitle>Session graph</DialogTitle>
          <DialogDescription>
            Which session started which, and the machine each one runs on.
          </DialogDescription>
        </DialogHeader>
        {open && (
          <ReactFlowProvider>
            <SessionGraph activeSessionId={activeSessionId} onOpened={() => onOpenChange(false)} />
          </ReactFlowProvider>
        )}
      </DialogContent>
    </Dialog>
  );
}

function SessionGraph({
  activeSessionId,
  onOpened,
}: {
  activeSessionId: string | null;
  onOpened: () => void;
}) {
  const navigate = useNavigate();
  const { sessions, loaded, error } = useCanvasSessions();
  const projects = useProjects().data;
  const hosts = useHosts().data;
  const activeProjectId = sessions.find((session) => session.id === activeSessionId)?.project_id;
  const [showAllProjects, setShowAllProjects] = useState(false);
  const scoped = Boolean(activeProjectId) && !showAllProjects;

  const graph = useMemo(() => {
    const projectNames = new Map(
      (projects ?? []).flatMap((project) =>
        project.id ? [[project.id, projectPathOf(project)] as const] : [],
      ),
    );
    const hostNames = new Map((hosts ?? []).map((host) => [host.host_id, host.name] as const));
    const shown = scoped
      ? sessions.filter((session) => session.project_id === activeProjectId)
      : sessions;
    return buildSessionGraph(shown, { hostNames, projectNames, activeId: activeSessionId });
  }, [activeProjectId, activeSessionId, hosts, projects, scoped, sessions]);

  const openSession = useCallback(
    (_event: unknown, node: Node) => {
      if (node.type !== "agent") return;
      navigate(`/c/${node.id}`);
      onOpened();
    },
    [navigate, onOpened],
  );

  return (
    <>
      {activeProjectId && (
        <div>
          <Button
            variant="outline"
            size="xs"
            data-testid="session-graph-scope"
            onClick={() => setShowAllProjects((current) => !current)}
          >
            {showAllProjects ? "This project only" : "All projects"}
          </Button>
        </div>
      )}
      {error && <div className="text-sm text-destructive">{error}</div>}
      {!loaded && <div className="text-sm text-muted-foreground">Loading sessions…</div>}
      <div className="min-h-0 flex-1 rounded-md border">
        <ReactFlow
          key={scoped ? "project" : "all"}
          nodes={[...graph.headings, ...graph.nodes]}
          edges={graph.edges}
          nodeTypes={nodeTypes}
          onNodeClick={openSession}
          fitView
          fitViewOptions={{ padding: 0.1, maxZoom: 1 }}
          panOnDrag
          panOnScroll
          zoomOnDoubleClick={false}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          proOptions={{ hideAttribution: true }}
          minZoom={0.1}
          maxZoom={3}
        >
          <Background bgColor="var(--card)" />
          <ZoomControls />
        </ReactFlow>
      </div>
    </>
  );
}
