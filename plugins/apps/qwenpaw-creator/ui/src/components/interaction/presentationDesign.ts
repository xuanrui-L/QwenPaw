import type {
  PresentationActionId,
  PresentationScreenId,
} from "@/contracts/creator";

// Behavioral protocol only. None of these entries supplies a visual design.
export const screens: {
  id: PresentationScreenId;
  label: string;
  purpose: string;
  required: PresentationActionId[];
}[] = [
  {
    id: "title",
    label: "首页",
    purpose: "作品介绍与观看入口",
    required: ["start", "map", "replay", "resume"],
  },
  {
    id: "play",
    label: "播放页",
    purpose: "视频画面、基本操作与剧情抉择",
    required: ["toggle_play", "map", "replay"],
  },
  {
    id: "map",
    label: "剧情地图",
    purpose: "剧情节点、分支关系与探索进度",
    required: ["map_back"],
  },
  {
    id: "ending",
    label: "结局页",
    purpose: "本次结局与再次观看入口",
    required: ["replay", "title"],
  },
];
export const actions: Record<
  PresentationActionId,
  { label: string; behavior: string }
> = {
  start: { label: "开始", behavior: "从故事入口开始播放" },
  resume: {
    label: "继续观看",
    behavior: "从上次访问的剧情节点继续；没有进度时禁用",
  },
  toggle_play: {
    label: "播放 / 暂停",
    behavior: "切换视频播放状态；剧情抉择期间保持暂停",
  },
  map: {
    label: "剧情地图",
    behavior: "打开剧情地图，并暂停视频和抉择倒计时",
  },
  map_back: { label: "返回", behavior: "关闭地图，回到此前的页面" },
  replay: {
    label: "重新开始",
    behavior: "重新播放故事入口，保留已探索的地图记录",
  },
  title: { label: "返回首页", behavior: "暂停播放并回到作品首页" },
  jump: { label: "回看节点", behavior: "只能跳转到已访问的剧情节点" },
  reset: { label: "清空进度", behavior: "清空观看与地图记录，回到首页" },
};
export type PreviewControl = {
  screen: PresentationScreenId;
  action: PresentationActionId;
  label: string;
};
