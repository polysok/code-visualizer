import { CvizService } from "./service";
import { SidebarControlPanel } from "../views/controlPanel";

/**
 * CViz keeps one database per repository (`<repo>/.code-visualizer/`). The MCP
 * server is started in the workspace folder and opens the database of the
 * repository containing it, so there is no context to choose: this only
 * refreshes the sidebar once the server is up.
 */
export class ContextManager {
  constructor(
    private readonly service: CvizService,
    private readonly sidebar: SidebarControlPanel
  ) {}

  public async initializeContext(): Promise<void> {
    await this.sidebar.refresh();
  }
}
