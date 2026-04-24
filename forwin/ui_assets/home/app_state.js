    const EXTENSION_BRIDGE_CHANNEL = 'forwin-publisher-extension';
    const BACKEND_EXTENSION_KEY_READY = @@EXTENSION_READY@@;
    const EXTENSION_INSTALL_PATH = @@EXTENSION_INSTALL_PATH@@;
    const MODEL_PROVIDER_PRESETS = @@MODEL_PROVIDER_PRESETS_JSON@@;
    const STAGE_ORDER = [
      'queued',
      'planning_arc',
      'creating_project',
      'resolving_arc_envelope',
      'running_provisional_preview',
      'provisional_failed',
      'assembling_context',
      'writing_chapter',
      'chapter_failed',
      'continuity_review',
      'applying_canon',
      'running_post_acceptance',
      'paused_for_review',
      'completed',
      'failed',
      'terminating',
      'cancelled',
    ];
    const GENESIS_STAGE_ORDER = ['brief', 'world', 'map', 'story_engine', 'book_blueprint', 'bootstrap'];
    const GENESIS_STAGE_FIELD_MAP = {
      brief: 'book_brief',
      world: 'world_bible',
      map: 'map_atlas',
      story_engine: 'story_engine',
      book_blueprint: 'book_arc_blueprint',
      bootstrap: 'execution_bootstrap',
    };
    const GENESIS_STAGE_FORM_FIELDS = {
      brief: [
        { path: 'title', label: '标题' },
        { path: 'one_line', label: '一句话概述', kind: 'textarea' },
        { path: 'audience', label: '目标读者' },
        { path: 'core_emotion', label: '核心情绪' },
        { path: 'core_delight', label: '核心爽点' },
        { path: 'promise', label: '读者承诺', kind: 'textarea' },
        { path: 'guardrails', label: '禁区', kind: 'list', help: '每行一条禁区或避雷要求。' },
      ],
      world: [
        { path: 'overview', label: '世界概览', kind: 'textarea' },
        { path: 'axioms', label: '世界规则', kind: 'list', help: '每行一条世界规则或世界公理。' },
        { path: 'history_slice', label: '历史切片', kind: 'textarea' },
        { path: 'naming_style', label: '命名风格' },
        { path: 'forbidden_zones', label: '禁区', kind: 'list', help: '每行一条世界禁区、禁忌或不允许出现的设定。' },
      ],
      map: [
        { path: 'overview', label: '地图概览', kind: 'textarea' },
        { path: 'topology_rules', label: '空间拓扑规则', kind: 'list', help: '每行一条移动、边界或空间成本规则。' },
      ],
      story_engine: [
        { path: 'relationship_axes', label: '关系轴', kind: 'list', help: '每行一条长期关系轴。' },
        { path: 'reader_promises', label: '读者承诺', kind: 'list', help: '每行一条要持续兑现的读者承诺。' },
        { path: 'long_arcs', label: '长期叙事引擎', kind: 'list', help: '每行一条长期推动线。' },
      ],
      book_blueprint: [
        { path: 'summary', label: '蓝图总览', kind: 'textarea' },
      ],
      bootstrap: [
        {
          path: 'operation_mode',
          label: '运行模式',
          kind: 'select',
          options: [{ value: 'blackbox', label: 'blackbox' }],
        },
        {
          path: 'start_policy',
          label: '启动策略',
          kind: 'select',
          options: [
            { value: 'explicit_start_writing_only', label: '显式点击“启动写作”后才开始' },
            { value: 'manual_handoff', label: '人工交接后再启动' },
          ],
        },
        { path: 'root_ready', label: '根层已准备就绪', kind: 'checkbox' },
      ],
    };
    const GENESIS_STAGE_ITEM_TARGETS = {
      world: [
        {
          path: 'axioms',
          label: '规则',
          singletonLabel: '规则集',
          template: ['新规则'],
          fields: [
            { path: '', label: '规则集', kind: 'list', help: '每行一条规则。' },
          ],
        },
        {
          path: 'history_slice',
          label: '历史',
          singletonLabel: '历史切片',
          template: '',
          fields: [
            { path: '', label: '历史切片', kind: 'textarea' },
          ],
        },
        {
          path: 'naming_style',
          label: '命名',
          singletonLabel: '命名风格',
          template: '',
          fields: [
            { path: '', label: '命名风格' },
          ],
        },
        {
          path: 'forbidden_zones',
          label: '禁区',
          singletonLabel: '禁区列表',
          template: [],
          fields: [
            { path: '', label: '禁区列表', kind: 'list', help: '每行一条禁区。' },
          ],
        },
        {
          collection: 'culture_profiles',
          label: '文化背景',
          template: {
            id: 'culture-new',
            name: '新文化背景',
            summary: '',
            inspiration: '',
            generator_civilization: '',
            generator_overlays: [],
            social_markers: [],
            aesthetic_keywords: [],
            character_name_style: '',
            region_name_style: '',
            location_name_style: '',
            character_name_examples: [],
            region_name_examples: [],
            location_name_examples: [],
            usage_notes: '',
          },
          fields: [
            { path: 'id', label: '文化 ID' },
            { path: 'name', label: '文化背景名' },
            { path: 'summary', label: '文化摘要', kind: 'textarea' },
            { path: 'inspiration', label: '文化母本 / 灵感来源' },
            { path: 'generator_civilization', label: '命名生成文明' },
            { path: 'generator_overlays', label: '命名叠加文明', kind: 'list', help: '每行一个叠加文明，如“基督教”或“穆斯林”。' },
            { path: 'social_markers', label: '社会特征', kind: 'list', help: '每行一条社会结构、价值观或礼制特征。' },
            { path: 'aesthetic_keywords', label: '审美关键词', kind: 'list', help: '每行一个视觉、气质或意象关键词。' },
            { path: 'character_name_style', label: '人物命名风格', kind: 'textarea' },
            { path: 'region_name_style', label: '地区命名风格', kind: 'textarea' },
            { path: 'location_name_style', label: '地点命名风格', kind: 'textarea' },
            { path: 'character_name_examples', label: '人物名字样例', kind: 'list', help: '每行一个人名样例。', name_generation_kind: 'person', name_generation_count: 8 },
            { path: 'region_name_examples', label: '地区名字样例', kind: 'list', help: '每行一个地区名样例。', name_generation_kind: 'region', name_generation_count: 8 },
            { path: 'location_name_examples', label: '地点名字样例', kind: 'list', help: '每行一个地点名样例。', name_generation_kind: 'place', name_generation_count: 8 },
            { path: 'usage_notes', label: '使用说明', kind: 'textarea' },
          ],
        },
      ],
      map: [
        {
          collection: 'submaps',
          label: '小世界',
          template: {
            name: '新小地图',
            scope: 'local_region',
            parent_scope: '',
            culture_profile_id: '',
            summary: '',
            culture_traits: [],
            climate: '',
            terrain: [],
            governing_power: '',
            resident_factions: [],
            key_locations: [],
            travel_rules: [],
            resource_themes: [],
          },
          fields: [
            { path: 'name', label: '子世界名', name_generation_kind: 'region', name_generation_count: 1 },
            {
              path: 'scope',
              label: '作用域',
              kind: 'select',
              options: [
                { value: 'macro_region', label: '宏观区域' },
                { value: 'nation', label: '国度 / 王朝' },
                { value: 'city_state', label: '城市 / 都城' },
                { value: 'sect_domain', label: '宗派 / 家族领地' },
                { value: 'local_region', label: '局部区域' },
                { value: 'frontier', label: '边境 / 前线' },
                { value: 'other', label: '其他' },
              ],
            },
            { path: 'parent_scope', label: '父作用域' },
            { path: 'culture_profile_id', label: '文化背景', kind: 'reference', source: 'culture_profiles' },
            { path: 'summary', label: '摘要', kind: 'textarea' },
            { path: 'culture_traits', label: '文化属性', kind: 'list', help: '每行一条文化特征。' },
            { path: 'climate', label: '气候特点' },
            { path: 'terrain', label: '地形特征', kind: 'list', help: '每行一条地形描述。' },
            { path: 'governing_power', label: '统治力量' },
            { path: 'resident_factions', label: '常驻势力', kind: 'list', help: '每行一个势力名。' },
            { path: 'key_locations', label: '关键地点', kind: 'list', help: '每行一个关键地点。' },
            { path: 'travel_rules', label: '通行规则', kind: 'list', help: '每行一条移动/交通规则。' },
            { path: 'resource_themes', label: '资源主题', kind: 'list', help: '每行一条资源或经济特征。' },
          ],
        },
        {
          collection: 'regions',
          label: '地区',
          template: {
            id: 'region-new',
            name: '新地区',
            subworld_name: '',
            parent_region_id: '',
            level: 1,
            culture_profile_id: '',
            kind: 'local_region',
            summary: '',
            culture_traits: [],
            climate: '',
            terrain: [],
            controller_factions: [],
            resource_themes: [],
          },
          fields: [
            { path: 'id', label: '地区 ID' },
            { path: 'name', label: '地区名', name_generation_kind: 'region', name_generation_count: 1 },
            { path: 'subworld_name', label: '所属小世界', kind: 'reference', source: 'submaps' },
            {
              path: 'level',
              label: '层级',
              kind: 'select',
              options: [
                { value: '1', label: '一级地区' },
                { value: '2', label: '二级地区' },
              ],
            },
            { path: 'parent_region_id', label: '父地区', kind: 'reference', source: 'regions' },
            { path: 'culture_profile_id', label: '文化背景', kind: 'reference', source: 'culture_profiles' },
            {
              path: 'kind',
              label: '地区类型',
              kind: 'select',
              options: [
                { value: 'local_region', label: '普通地区' },
                { value: 'nation_core', label: '国度核心区' },
                { value: 'district', label: '行政区 / 城区' },
                { value: 'sect_domain', label: '宗派领地' },
                { value: 'family_domain', label: '家族领地' },
                { value: 'frontier_zone', label: '边境区' },
                { value: 'other', label: '其他' },
              ],
            },
            { path: 'summary', label: '摘要', kind: 'textarea' },
            { path: 'culture_traits', label: '文化属性', kind: 'list', help: '每行一条文化特征。' },
            { path: 'climate', label: '气候特点' },
            { path: 'terrain', label: '地形特征', kind: 'list', help: '每行一条地形描述。' },
            { path: 'controller_factions', label: '控制势力', kind: 'list', help: '每行一个势力名。' },
            { path: 'resource_themes', label: '资源主题', kind: 'list', help: '每行一条资源或经济特征。' },
          ],
        },
        {
          collection: 'nodes',
          label: '地点节点',
          template: {
            name: '新地点',
            kind: 'region',
            parent_subworld: '',
            parent_region_id: '',
            culture_profile_id: '',
            description: '',
            control: '',
            danger: '',
            climate_note: '',
            terrain_note: '',
            culture_note: '',
            resources: [],
          },
          fields: [
            { path: 'name', label: '地点名', name_generation_kind: 'place', name_generation_count: 1 },
            {
              path: 'kind',
              label: '地点类型',
              kind: 'select',
              options: [
                { value: 'region', label: '区域' },
                { value: 'city', label: '城市' },
                { value: 'sect', label: '宗派驻地' },
                { value: 'fortress', label: '要塞' },
                { value: 'frontier', label: '边境' },
                { value: 'ruin', label: '遗迹' },
                { value: 'other', label: '其他' },
              ],
            },
            { path: 'parent_subworld', label: '所属子世界', kind: 'reference', source: 'submaps' },
            { path: 'parent_region_id', label: '所属地区', kind: 'reference', source: 'regions' },
            { path: 'culture_profile_id', label: '文化背景', kind: 'reference', source: 'culture_profiles' },
            { path: 'description', label: '地点描述', kind: 'textarea' },
            { path: 'control', label: '控制方' },
            { path: 'danger', label: '危险度 / 风险' },
            { path: 'climate_note', label: '气候备注' },
            { path: 'terrain_note', label: '地形备注' },
            { path: 'culture_note', label: '文化备注' },
            { path: 'resources', label: '资源', kind: 'list', help: '每行一条资源或地点价值。' },
          ],
        },
      ],
      story_engine: [
        {
          collection: 'core_cast',
          label: '角色',
          template: {
            name: '新角色',
            role: '',
            desire: '',
            fear: '',
            secret: '',
            culture_profile_id: '',
            home_subworld: '',
            home_region: '',
            home_location: '',
            current_region: '',
            current_base: '',
            affiliated_faction: '',
            affiliated_family: '',
            faction_memberships: [],
          },
          fields: [
            { path: 'name', label: '角色名', name_generation_kind: 'person', name_generation_count: 1 },
            { path: 'role', label: '角色定位' },
            { path: 'desire', label: '欲望 / 目标', kind: 'textarea' },
            { path: 'fear', label: '恐惧 / 风险', kind: 'textarea' },
            { path: 'secret', label: '秘密', kind: 'textarea' },
            { path: 'culture_profile_id', label: '文化背景', kind: 'reference', source: 'culture_profiles' },
            { path: 'home_subworld', label: '家乡所在子世界', kind: 'reference', source: 'submaps' },
            { path: 'home_region', label: '家乡所在地区', kind: 'reference', source: 'regions' },
            { path: 'home_location', label: '家乡地点', kind: 'reference', source: 'nodes' },
            { path: 'current_region', label: '当前活动地区', kind: 'reference', source: 'regions' },
            { path: 'current_base', label: '当前据点', kind: 'reference', source: 'nodes' },
            { path: 'affiliated_faction', label: '隶属势力', kind: 'reference', source: 'factions' },
            { path: 'affiliated_family', label: '隶属家族 / 宗派' },
            {
              path: 'faction_memberships',
              label: '势力归属',
              kind: 'object_list',
              row_label: '每行：势力名 | 关系 | 身份/阶位 | primary(true/false)',
              schema: [
                { key: 'faction_name', default: '' },
                { key: 'relation', default: 'member' },
                { key: 'rank', default: '' },
                { key: 'is_primary', default: false, type: 'boolean' },
              ],
            },
          ],
        },
        {
          collection: 'factions',
          label: '势力',
          template: {
            name: '新势力',
            role: '',
            goal: '',
            leverage: '',
            relationship_to_protagonist: '',
            culture_profile_id: '',
            base_subworld: '',
            headquarters_region: '',
            base_location: '',
            territory_scope: [],
            culture_keywords: [],
            footprint: [],
          },
          fields: [
            { path: 'name', label: '势力名', name_generation_kind: 'epithet', name_generation_count: 1 },
            { path: 'role', label: '势力定位' },
            { path: 'goal', label: '目标', kind: 'textarea' },
            { path: 'leverage', label: '筹码 / 杠杆', kind: 'textarea' },
            { path: 'relationship_to_protagonist', label: '与主角关系', kind: 'textarea' },
            { path: 'culture_profile_id', label: '文化背景', kind: 'reference', source: 'culture_profiles' },
            { path: 'base_subworld', label: '根据地子世界', kind: 'reference', source: 'submaps' },
            { path: 'headquarters_region', label: '总部地区', kind: 'reference', source: 'regions' },
            { path: 'base_location', label: '核心地点', kind: 'reference', source: 'nodes' },
            { path: 'territory_scope', label: '势力范围', kind: 'list', help: '每行一个范围或所属区域。' },
            { path: 'culture_keywords', label: '势力文化关键词', kind: 'list', help: '每行一个文化/作风关键词。' },
            {
              path: 'footprint',
              label: '分布足迹',
              kind: 'object_list',
              row_label: '每行：小世界 | 地区ID | 覆盖强度 | 模式',
              schema: [
                { key: 'subworld_name', default: '' },
                { key: 'region_id', default: '' },
                { key: 'presence', default: 'medium' },
                { key: 'mode', default: 'rule' },
              ],
            },
          ],
        },
        {
          collection: 'opposition',
          label: '对手盘',
          template: {
            name: '新对手',
            role: '',
            desire: '',
            pressure: '',
            relationship_to_protagonist: '',
            culture_profile_id: '',
            base_subworld: '',
            base_region: '',
            base_location: '',
            backing_faction: '',
            backing_factions: [],
          },
          fields: [
            { path: 'name', label: '对手名', name_generation_kind: 'person', name_generation_count: 1 },
            { path: 'role', label: '对手定位' },
            { path: 'desire', label: '欲望 / 主张', kind: 'textarea' },
            { path: 'pressure', label: '施压方式', kind: 'textarea' },
            { path: 'relationship_to_protagonist', label: '与主角关系', kind: 'textarea' },
            { path: 'culture_profile_id', label: '文化背景', kind: 'reference', source: 'culture_profiles' },
            { path: 'base_subworld', label: '主要活动子世界', kind: 'reference', source: 'submaps' },
            { path: 'base_region', label: '主要活动地区', kind: 'reference', source: 'regions' },
            { path: 'base_location', label: '主要地点', kind: 'reference', source: 'nodes' },
            { path: 'backing_faction', label: '背后势力', kind: 'reference', source: 'factions' },
            { path: 'backing_factions', label: '背后势力列表', kind: 'list', help: '每行一个势力名。' },
          ],
        },
      ],
      book_blueprint: [
        {
          collection: 'arcs',
          label: 'Arc 蓝图',
          template: {
            arc_number: 1,
            title: '新 Arc',
            arc_synopsis: '',
            goal: '',
            stakes: '',
            payoff_direction: '',
            chapter_start: 1,
            chapter_end: 3,
            chapter_count: 3,
            target_size: 3,
            soft_min: 2,
            soft_max: 4,
          },
          fields: [
            { path: 'arc_number', label: 'Arc 编号', kind: 'number' },
            { path: 'title', label: 'Arc 标题' },
            { path: 'arc_synopsis', label: 'Arc 摘要', kind: 'textarea' },
            { path: 'goal', label: '阶段目标', kind: 'textarea' },
            { path: 'stakes', label: '风险 / 代价', kind: 'textarea' },
            { path: 'payoff_direction', label: '兑现方向' },
            { path: 'chapter_start', label: '起始章节', kind: 'number' },
            { path: 'chapter_end', label: '结束章节', kind: 'number' },
            { path: 'chapter_count', label: '章节数', kind: 'number' },
            { path: 'target_size', label: '目标 size', kind: 'number' },
            { path: 'soft_min', label: 'soft_min', kind: 'number' },
            { path: 'soft_max', label: 'soft_max', kind: 'number' },
          ],
        },
      ],
    };
    const TERMINAL_TASK_STATUSES = new Set(['completed', 'partial_failed', 'failed', 'needs_review', 'cancelled', 'paused', 'succeeded']);
    const ACTIVE_TASK_STATUSES = new Set(['starting', 'running', 'pending', 'terminating']);
    const pendingBridgeRequests = new Map();
    let settingsState = null;
    let platformsState = [];
    let booksState = [];
    let taskCenterState = [];
    let selectedBookIds = new Set();
    let selectedTaskKeys = new Set();
    let currentProfileId = '';
    let currentTaskModalKind = 'generation';
    let currentTaskPrefill = {};
    let currentGovernanceAction = null;
    let currentDrawerTask = null;
    let currentDrawerSignature = '';
    let drawerRequestToken = 0;
    let taskPollHasActive = false;
    let booksStateSignature = '';
    let taskCenterStateSignature = '';
    let taskCenterBookImpactSignature = '';
    let booksRefreshPending = false;
    let currentHomeTab = 'book';
    let currentGenesisProjectId = '';
    let currentGenesisDetail = null;
    let currentGenesisStage = 'brief';
    let currentGenesisItemCollection = '';
    let currentGenesisItemIndex = -1;
    let currentGenesisModelProfileId = '';
    let currentGenesisDrafts = {};
    let genesisActionBusy = false;

@@PAGE_DOM_HELPERS_JS@@

    function setGlobalStatus(text, title = '系统状态') {
      document.getElementById('global_status_title').textContent = title;
      document.getElementById('global_status').textContent = text;
    }

    function taskSelectionKey(item) {
      return `${item.task_kind}:${item.task_id}`;
    }

    function normalizeForSignature(value) {
      if (Array.isArray(value)) return value.map((item) => normalizeForSignature(item));
      if (value && typeof value === 'object') {
        const normalized = {};
        Object.keys(value).sort().forEach((key) => {
          normalized[key] = normalizeForSignature(value[key]);
        });
        return normalized;
      }
      return value;
    }

    function dataSignature(value) {
      return JSON.stringify(normalizeForSignature(value));
    }

    function taskModalOpen() {
      return Boolean(document.getElementById('task_modal_shell')?.classList.contains('open'));
    }

    function numberListFingerprint(values) {
      return (Array.isArray(values) ? values : [])
        .map((value) => Number(value || 0))
        .filter(Boolean)
        .sort((left, right) => left - right)
        .join(',');
    }

    function generationControlFingerprint(control = {}) {
      return [
        String(control.plan_state || ''),
        String(control.writing_state || ''),
        String(control.review_state || ''),
        String(control.blocking_reason?.code || ''),
        String(control.next_gate || ''),
        Number(control.current_chapter || 0),
        Number(control.next_chapter || 0),
        Number(control.chapters_until_review || 0),
        Number(control.chapters_until_replan_eligible || 0),
        control.can_resume ? '1' : '0',
        numberListFingerprint(control.accepted_chapters),
        numberListFingerprint(control.drafted_chapters),
        numberListFingerprint(control.generated_chapters),
        numberListFingerprint(control.pending_review_chapters),
        numberListFingerprint(control.failed_chapters),
      ].join('|');
    }

    function bookListFingerprint(books = []) {
      return (Array.isArray(books) ? books : []).map((book) => [
        String(book.id || ''),
        String(book.updated_at || ''),
        String(book.creation_status || ''),
        String(book.title || ''),
        Number(book.chapter_count || 0),
        Number(book.needs_review_chapter_count || 0),
        Number(book.target_total_chapters || 0),
        generationControlFingerprint(book.generation_control || {}),
      ].join('|')).join('\n');
    }

    function taskCenterFingerprint(items = []) {
      return (Array.isArray(items) ? items : []).map((item) => [
        String(item.task_kind || ''),
        String(item.task_id || ''),
        String(item.project_id || ''),
        String(item.status || ''),
        String(item.updated_at || ''),
        String(item.current_stage || ''),
        Number(item.current_chapter || 0),
        String(item.message || ''),
        String(item.error || ''),
        numberListFingerprint(item.completed_chapters),
        numberListFingerprint(item.paused_chapters),
        numberListFingerprint(item.failed_chapters),
        generationControlFingerprint(item.generation_control || {}),
      ].join('|')).join('\n');
    }

    function taskCenterBookImpactFingerprint(items = []) {
      return (Array.isArray(items) ? items : [])
        .filter((item) => item?.task_kind === 'generation' && item?.project_id)
        .map((item) => [
          String(item.project_id || ''),
          String(item.status || ''),
          String(item.current_stage || ''),
          Number(item.current_chapter || 0),
          numberListFingerprint(item.completed_chapters),
          numberListFingerprint(item.paused_chapters),
          numberListFingerprint(item.failed_chapters),
          generationControlFingerprint(item.generation_control || {}),
        ].join('|'))
        .join('\n');
    }

    async function runGenesisAction(fn, busyMessage = 'Genesis 正在执行上一条操作，请稍候。') {
      if (genesisActionBusy) {
        setGlobalStatus(busyMessage, 'Genesis 工作台');
        return null;
      }
      genesisActionBusy = true;
      try {
        return await fn();
      } finally {
        genesisActionBusy = false;
      }
    }

    function populateModelProfileSelect(selectEl, preferredId = '') {
      if (!selectEl) return '';
      clearNode(selectEl);
      const profiles = Array.isArray(settingsState?.profiles) ? settingsState.profiles : [];
      if (!profiles.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '暂无模型配置';
        selectEl.appendChild(option);
        selectEl.disabled = true;
        return '';
      }
      const selectedId = preferredId || settingsState?.default_profile_id || profiles[0]?.id || '';
      profiles.forEach((profile, index) => {
        const option = document.createElement('option');
        option.value = profile.id;
        option.textContent = `${profile.name}${profile.id === settingsState.default_profile_id ? ' · 默认' : ''}`;
        option.selected = profile.id === selectedId || (!selectedId && index === 0);
        selectEl.appendChild(option);
      });
      selectEl.disabled = false;
      return selectEl.value || selectedId || '';
    }

    function syncBookBulkActions() {
      const selectableCount = booksState.length;
      const selectedCount = selectedBookIds.size;
      const selectAllBtn = document.getElementById('book_select_all_btn');
      const bulkDeleteBtn = document.getElementById('book_bulk_delete_btn');
      if (selectAllBtn) {
        selectAllBtn.disabled = selectableCount === 0;
        selectAllBtn.textContent = selectableCount > 0 && selectedCount === selectableCount ? '取消全选' : '全选';
      }
      if (bulkDeleteBtn) {
        bulkDeleteBtn.disabled = selectedCount === 0;
        bulkDeleteBtn.textContent = selectedCount > 0 ? `批量删除（${selectedCount}）` : '批量删除';
      }
    }

    function syncTaskBulkActions() {
      const selectableCount = taskCenterState.filter((item) => item.deletable).length;
      const selectedCount = selectedTaskKeys.size;
      const selectAllBtn = document.getElementById('task_select_all_btn');
      const bulkDeleteBtn = document.getElementById('task_bulk_delete_btn');
      if (selectAllBtn) {
        selectAllBtn.disabled = selectableCount === 0;
        selectAllBtn.textContent = selectableCount > 0 && selectedCount === selectableCount ? '取消全选' : '全选可删';
      }
      if (bulkDeleteBtn) {
        bulkDeleteBtn.disabled = selectedCount === 0;
        bulkDeleteBtn.textContent = selectedCount > 0 ? `批量删除（${selectedCount}）` : '批量删除';
      }
    }

    function bridgeId() {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
      return `forwin-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function bridgeRequest(action, payload = {}, timeoutMs = 1800) {
      return new Promise((resolve, reject) => {
        const correlationId = bridgeId();
        const timer = window.setTimeout(() => {
          pendingBridgeRequests.delete(correlationId);
          reject(new Error('浏览器扩展未响应。'));
        }, timeoutMs);
        pendingBridgeRequests.set(correlationId, { resolve, reject, timer });
        window.postMessage(
          {
            channel: EXTENSION_BRIDGE_CHANNEL,
            direction: 'page-to-extension',
            kind: 'request',
            correlationId,
            action,
            payload,
          },
          window.location.origin,
        );
      });
    }

    function switchTab(tab) {
      currentHomeTab = ['book', 'task', 'config'].includes(tab) ? tab : 'book';
      const bookActive = currentHomeTab === 'book';
      const taskActive = currentHomeTab === 'task';
      document.getElementById('tab_book').classList.toggle('active', bookActive);
      document.getElementById('tab_task').classList.toggle('active', taskActive);
      document.getElementById('tab_config').classList.toggle('active', currentHomeTab === 'config');
      document.getElementById('panel_book').classList.toggle('active', bookActive);
      document.getElementById('panel_task').classList.toggle('active', taskActive);
      document.getElementById('panel_config').classList.toggle('active', currentHomeTab === 'config');
      if (bookActive && booksRefreshPending) {
        booksRefreshPending = false;
        void loadBooks();
      }
      if (taskActive && !document.getElementById('task_list')?.childNodes.length) {
        void loadTaskCenter();
      }
      if (currentHomeTab === 'config') {
        void loadPlatforms();
      }
    }

    function badgeKindByStatus(status) {
      if (['completed', 'succeeded', 'cancelled', 'accepted'].includes(status)) return 'ok';
      if (['failed', 'partial_failed'].includes(status)) return 'danger';
      if (['needs_review', 'terminating', 'pending', 'running', 'drafted', 'paused'].includes(status)) return 'warn';
      return '';
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail ?? payload.message ?? `HTTP ${response.status}`;
        let message = '';
        if (typeof detail === 'string') {
          message = detail;
        } else if (Array.isArray(detail)) {
          message = detail.map((item) => {
            if (typeof item === 'string') return item;
            if (item && typeof item === 'object') {
              const loc = Array.isArray(item.loc) ? item.loc.join('.') : '';
              const msg = typeof item.msg === 'string' ? item.msg : JSON.stringify(item);
              return loc ? `${loc}: ${msg}` : msg;
            }
            return String(item);
          }).join('；');
        } else if (detail && typeof detail === 'object') {
          message = JSON.stringify(detail, null, 2);
        } else {
          message = String(detail);
        }
        throw new Error(message);
      }
      return payload;
    }

    function parseTextareaLines(value) {
      return String(value || '')
        .split(/\\r?\\n/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function deepCloneJson(value) {
      return JSON.parse(JSON.stringify(value ?? {}));
    }

    function serializeTaskType(kind) {
      return kind === 'upload' ? '上传' : '生成';
    }

    function stageLabel(stage) {
      const map = {
        queued: '排队',
        planning_arc: '规划大纲',
        creating_project: '创建项目',
        resolving_arc_envelope: '解析 Arc Envelope',
        running_provisional_preview: 'Provisional 预演',
        provisional_failed: 'Provisional 失败',
        assembling_context: '组装上下文',
        writing_chapter: '写作章节',
        chapter_failed: '章节失败',
        continuity_review: '连续性审查',
        applying_canon: '写入 Canon',
        running_post_acceptance: '后置处理',
        paused_for_review: '等待人工检查',
        completed: '完成',
        failed: '失败',
        terminating: '终止中',
        cancelled: '已取消',
        paused: '已安全暂停',
      };
      return map[stage] || stage || '未知阶段';
    }

    function chapterStatusLabel(status) {
      const map = {
        planned: '待生成正文',
        running: '生成中',
        drafted: '已出正文',
        accepted: '已写入 Canon',
        needs_review: '待人工检查',
        failed: '生成失败',
        completed: '已完成',
      };
      return map[status] || status || '未知状态';
    }
