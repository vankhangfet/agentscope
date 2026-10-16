import {
	BookText,
	BotMessageSquare,
	Cable,
	Calendars,
	Compass,
	KeyRound,
	Languages,
	LibraryBig,
	MessagesSquare,
	UserRound,
	Workflow,
} from 'lucide-react';
import { useOnborda } from 'onborda';
import { Link, useNavigate, useLocation } from 'react-router-dom';

import AgentScope from '@/assets/images/agentscope_mono.svg?react';
import MCPSvg from '@/assets/images/mcp.svg?react';
import { CHAT_TOUR_NAME } from '@/components/tour/chatTourSteps';
import {
	Sidebar,
	SidebarContent,
	SidebarFooter,
	SidebarGroup,
	SidebarGroupContent,
	SidebarHeader,
	SidebarMenu,
	SidebarMenuButton,
	SidebarMenuItem,
} from '@/components/ui/sidebar';
import { useWorkflowRuns } from '@/hooks/useWorkflowRuns';
import i18n from '@/i18n';
import { useTranslation } from '@/i18n/useI18n';
import { parkedNodes } from '@/pages/workflow/components/runReadiness';

export function AppSidebar() {
	const navigate = useNavigate();
	const location = useLocation();
	const { t } = useTranslation();
	const { startOnborda } = useOnborda();

	const handleStartTour = () => {
		if (!location.pathname.startsWith('/chat')) {
			// Page not mounted yet — leave a flag, navigate, and let the
			// ChatTourController auto-trigger after ChatPage mounts.
			sessionStorage.setItem('force_tour', '1');
			navigate('/chat');
		} else {
			startOnborda(CHAT_TOUR_NAME);
		}
	};

	const handleToggleLanguage = () => {
		const next = i18n.language.startsWith('zh') ? 'en' : 'zh';
		i18n.changeLanguage(next);
	};

	// Pending approval count for the workflow icon's chip: how many
	// nodes across all parked runs await a verdict. Shares the query
	// with the approvals page (react-query dedupes by key — its 15s
	// interval wins whenever it is mounted) and polls once a minute on
	// its own, so the chip still updates for scheduled runs and
	// other-tab verdicts while the approvals page is unmounted.
	const { runs } = useWorkflowRuns(
		{ status: 'WAITING_HUMAN' },
		{ refetchInterval: 60_000 },
	);
	const pendingApprovals = runs.reduce(
		(total, run) => total + parkedNodes(run.data).length,
		0,
	);

	return (
		<Sidebar
			collapsible="none"
			className="w-[calc(var(--sidebar-width-icon)+1px)]! bg-transparent"
		>
			<SidebarHeader>
				<div className="flex items-center justify-center size-8 mt-2 rounded-full bg-primary">
					<AgentScope className="size-5 items-center justify-center rounded-lg text-primary-foreground" />
				</div>
			</SidebarHeader>
			<SidebarContent>
				<SidebarGroup>
					<SidebarGroupContent>
						<SidebarMenu>
							<SidebarMenuItem key={'chat'}>
								<SidebarMenuButton
									tooltip={{ children: t('common.chat'), hidden: false }}
									isActive={
										location.pathname === '/chat' ||
										location.pathname.startsWith('/chat/')
									}
									onClick={() => navigate('/chat')}
									className="justify-center"
								>
									<BotMessageSquare />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.schedule'), hidden: false }}
									isActive={location.pathname === '/schedule'}
									onClick={() => navigate('/schedule')}
									className="justify-center"
								>
									<Calendars />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.workflow'), hidden: false }}
									// Stays lit while on a builder / runs / approvals
									// subroute, mirroring the MCP entry.
									isActive={location.pathname.startsWith('/workflow')}
									onClick={() => navigate('/workflow')}
									className="justify-center"
								>
									<Workflow />
								</SidebarMenuButton>
								{/* The chip overlays the button's corner on the
								 * item (its relative anchor — the button clips
								 * overflow) and is itself the one nav entry to
								 * the approvals inbox. The link pads to a 24px
								 * hit area; the visible pill inside stays 16px. */}
								{pendingApprovals > 0 && (
									<Link
										to="/workflow/approvals"
										aria-label={t('workflow.approvals.chipLabel', {
											count: pendingApprovals,
										})}
										title={t('workflow.approvals.chipLabel', {
											count: pendingApprovals,
										})}
										className="group absolute top-0 right-0 flex size-6 items-center justify-center"
									>
										<span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[9px] leading-none font-semibold text-white tabular-nums group-hover:bg-amber-600">
											{pendingApprovals > 9 ? '9+' : pendingApprovals}
										</span>
									</Link>
								)}
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.discussion'), hidden: false }}
									// Stays lit while on a discussion's live view,
									// mirroring the workflow entry.
									isActive={location.pathname.startsWith('/discussion')}
									onClick={() => navigate('/discussion')}
									className="justify-center"
								>
									<MessagesSquare />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.channel'), hidden: false }}
									isActive={location.pathname === '/channel'}
									onClick={() => navigate('/channel')}
									className="px-2"
								>
									<Cable />
								</SidebarMenuButton>
							</SidebarMenuItem>
						</SidebarMenu>
					</SidebarGroupContent>
				</SidebarGroup>
				<SidebarGroup>
					<SidebarGroupContent>
						<SidebarMenu>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.credential'), hidden: false }}
									isActive={location.pathname === '/credential'}
									onClick={() => navigate('/credential')}
									className="justify-center"
								>
									<KeyRound />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.mcp-hub'), hidden: false }}
									// Stays lit while browsing a hub under /mcp/:hubId.
									isActive={location.pathname.startsWith('/mcp')}
									onClick={() => navigate('/mcp')}
									className="justify-center"
								>
									<MCPSvg />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.skill-hub'), hidden: false }}
									isActive={location.pathname.startsWith('/skill')}
									onClick={() => navigate('/skill')}
									className="justify-center"
								>
									<BookText />
								</SidebarMenuButton>
							</SidebarMenuItem>
							<SidebarMenuItem>
								<SidebarMenuButton
									tooltip={{ children: t('common.knowledge'), hidden: false }}
									isActive={location.pathname === '/knowledge'}
									onClick={() => navigate('/knowledge')}
									className="justify-center"
								>
									<LibraryBig />
								</SidebarMenuButton>
							</SidebarMenuItem>
						</SidebarMenu>
					</SidebarGroupContent>
				</SidebarGroup>
			</SidebarContent>
			<SidebarFooter>
				<SidebarMenu>
					<SidebarMenuItem>
						<SidebarMenuButton
							tooltip={{
								children: i18n.language.startsWith('zh')
									? t('common.switchToEn')
									: t('common.switchToZh'),
								hidden: false,
							}}
							onClick={handleToggleLanguage}
							className="justify-center"
						>
							<Languages />
						</SidebarMenuButton>
					</SidebarMenuItem>
					<SidebarMenuItem>
						<SidebarMenuButton
							tooltip={{ children: t('tour.trigger'), hidden: false }}
							onClick={handleStartTour}
							className="justify-center"
						>
							<Compass />
						</SidebarMenuButton>
					</SidebarMenuItem>
					<SidebarMenuItem>
						<SidebarMenuButton
							tooltip={{ children: t('common.settings'), hidden: false }}
							isActive={location.pathname === '/setup'}
							onClick={() => navigate('/setup')}
							className="justify-center"
						>
							<UserRound />
						</SidebarMenuButton>
					</SidebarMenuItem>
				</SidebarMenu>
			</SidebarFooter>
		</Sidebar>
	);
}
