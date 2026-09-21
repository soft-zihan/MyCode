import { useState, useEffect, useCallback } from 'react';
import { fetchSkills, fetchSkillEvolutionStatus, fetchReplayPool, fetchChampion, fetchSkillProvenance, deleteSkill, Skill, SkillEvolutionStatus, ReplayPoolData, ChampionData } from '../api/client';
import { Wrench, RefreshCw, Activity, Edit3, Save, X, User, Folder, Trash2, Power, Star, CheckCircle, AlertTriangle, EyeOff, Circle } from 'lucide-react';
import { PageLayout } from '../components/PageLayout';

interface SkillDetail extends Skill {
  prompt_template?: string;
  raw_content?: string;
}

function SkillEvolutionDetail({
  skillName,
  status,
  replayPool,
  champion,
  provenance,
  getStatusBadge,
}: {
  skillName: string;
  status: SkillEvolutionStatus;
  replayPool: ReplayPoolData | null;
  champion: ChampionData | null;
  provenance: any[];
  getStatusBadge: (s: string) => React.ReactNode;
}) {
  const [expandedProv, setExpandedProv] = useState<number | null>(null);

  return (
    <div className="space-y-4 border border-gray-200 rounded-lg p-4 bg-white">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-bold text-gray-900">{skillName}</h3>
          {getStatusBadge(status.status)}
        </div>
        {status.current_version && (
          <span className="text-xs text-gray-500 font-mono">v{status.current_version}</span>
        )}
      </div>

      {status.reasons.length > 0 && (
        <div className="text-xs text-gray-500 bg-gray-50 rounded p-2">
          {status.reasons.map((r, i) => <span key={i}>{r}{i < status.reasons.length - 1 ? ' · ' : ''}</span>)}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        <div>
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Pass Rate</h4>
          <div className="flex items-center justify-between text-xs">
            <span className="text-gray-500">Replay pass rate</span>
            <span className="font-mono font-medium text-gray-700">
              {((status.rule_summary?.pass_rate || 0) * 100).toFixed(0)}%
            </span>
          </div>
        </div>

        <div>
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Evaluation Rules</h4>
          <div className="space-y-1">
            {(status.rule_summary?.outcomes || []).map((outcome: any, idx: number) => (
              <div key={idx} className="flex items-center justify-between text-xs">
                <span className="text-gray-600 truncate max-w-[160px]">{outcome.label || outcome.rule_id}</span>
                <span className={outcome.passed ? 'text-green-600' : 'text-red-500'}>
                  {outcome.passed ? 'PASS' : 'FAIL'}
                </span>
              </div>
            ))}
            {(!status.rule_summary?.outcomes || status.rule_summary.outcomes.length === 0) && (
              <div className="text-xs text-gray-400">No rule outcomes</div>
            )}
          </div>
          <div className="mt-2 pt-2 border-t border-gray-100 flex items-center justify-between text-xs">
            <span className="text-gray-500">Hard failures</span>
            <span className={`font-mono font-medium ${(status.rule_summary?.hard_failures || 0) > 0 ? 'text-red-600' : 'text-green-600'}`}>
              {status.rule_summary?.hard_failures || 0}
            </span>
          </div>
        </div>
      </div>

      {replayPool && replayPool.total_samples > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
            Replay Pool ({replayPool.total_samples} samples: {replayPool.dev_count} dev / {replayPool.test_count} test)
          </h4>
          <div className="max-h-40 overflow-y-auto space-y-1">
            {replayPool.samples.map((sample, idx) => (
              <div key={idx} className="flex items-center gap-2 text-[11px] py-1 px-2 bg-gray-50 rounded">
                <span className={sample.ok ? 'text-green-500' : 'text-red-500'}>{sample.ok ? '✓' : '✗'}</span>
                <span className="text-gray-500 font-mono">{sample.source_type}</span>
                <span className={`px-1 py-0.5 rounded text-[10px] ${
                  sample.split === 'promotion_test' ? 'bg-purple-100 text-purple-700' : 'bg-indigo-100 text-indigo-700'
                }`}>{sample.split === 'promotion_test' ? 'test' : 'dev'}</span>
                <span className="text-gray-600 truncate flex-1">{sample.latest_user}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {champion?.has_champion && champion.champion && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
          <h4 className="text-xs font-semibold text-yellow-800 mb-2 flex items-center gap-1">
            <Star className="w-3 h-3" /> Champion Version
          </h4>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div><span className="text-gray-500">Version:</span> <span className="font-mono font-medium">{champion.champion.version}</span></div>
            <div><span className="text-gray-500">Score:</span> <span className="font-mono font-medium">{champion.champion.average_score?.toFixed(2)}</span></div>
            <div><span className="text-gray-500">Hard failures:</span> <span className="font-mono font-medium">{champion.champion.hard_failures}</span></div>
            <div><span className="text-gray-500">Promoted:</span> <span className="font-mono">{champion.champion.promoted_at}</span></div>
          </div>
        </div>
      )}

      {provenance.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
            Evolution Timeline ({provenance.length} events)
          </h4>
          <div className="relative pl-4 border-l-2 border-gray-200 space-y-2 max-h-64 overflow-y-auto">
            {provenance.map((entry, idx) => (
              <div key={idx} className="relative">
                <div className="absolute -left-[21px] top-1 w-3 h-3 rounded-full bg-indigo-400 border-2 border-white" />
                <div
                  className="text-xs cursor-pointer hover:bg-gray-50 rounded p-1.5"
                  onClick={() => setExpandedProv(expandedProv === idx ? null : idx)}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-gray-500 font-mono text-[10px]">{entry.time || ''}</span>
                    <span className="px-1.5 py-0.5 bg-indigo-100 text-indigo-700 rounded text-[10px]">{entry.action || 'event'}</span>
                    {entry.version && <span className="font-mono text-gray-600">v{entry.version}</span>}
                  </div>
                  {entry.description && <div className="text-gray-600 mt-0.5">{entry.description}</div>}
                  {expandedProv === idx && (
                    <pre className="mt-1 text-[10px] font-mono text-gray-500 bg-gray-50 p-2 rounded overflow-x-auto">
                      {JSON.stringify(entry, null, 2)}
                    </pre>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<SkillDetail | null>(null);
  const [editContent, setEditContent] = useState<string>('');
  const [isEditing, setIsEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'skills' | 'evolution'>('skills');
  const [filterSource, setFilterSource] = useState<'all' | 'user' | 'project'>('all');
  const [disabledSkills, setDisabledSkills] = useState<Set<string>>(new Set());
  const [evoSkillStatuses, setEvoSkillStatuses] = useState<Map<string, SkillEvolutionStatus>>(new Map());
  const [evoSelectedSkill, setEvoSelectedSkill] = useState<string | null>(null);
  const [evoReplayPool, setEvoReplayPool] = useState<ReplayPoolData | null>(null);
  const [evoChampion, setEvoChampion] = useState<ChampionData | null>(null);
  const [evoProvenance, setEvoProvenance] = useState<any[]>([]);
  const [evoLoading, setEvoLoading] = useState(false);

  // Load disabled skills from localStorage
  useEffect(() => {
    const saved = localStorage.getItem('disabledSkills');
    if (saved) {
      setDisabledSkills(new Set(JSON.parse(saved)));
    }
  }, []);

  const toggleSkill = (skillName: string) => {
    const newDisabled = new Set(disabledSkills);
    if (newDisabled.has(skillName)) {
      newDisabled.delete(skillName);
    } else {
      newDisabled.add(skillName);
    }
    setDisabledSkills(newDisabled);
    localStorage.setItem('disabledSkills', JSON.stringify([...newDisabled]));
  };

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const skillsData = await fetchSkills();
      setSkills(skillsData);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load data');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleSkillClick = async (skill: Skill) => {
    try {
      const response = await fetch(`/api/skills/${skill.name}`);
      const detail: SkillDetail = await response.json();
      setSelectedSkill(detail);
      setEditContent(detail.raw_content || '');
      setIsEditing(false);
    } catch (err) {
      console.error('Failed to load skill detail:', err);
      setSelectedSkill({ ...skill, raw_content: '', prompt_template: '' });
      setEditContent('');
      setIsEditing(false);
    }
  };

  const handleSave = async () => {
    if (!selectedSkill) return;
    setSaving(true);
    try {
      const response = await fetch(`/api/skills/${selectedSkill.name}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editContent }),
      });
      if (response.ok) {
        setIsEditing(false);
        setSelectedSkill({ ...selectedSkill, raw_content: editContent });
        await loadData();
      }
    } catch (err) {
      console.error('Failed to save skill:', err);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (skillName: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm(`Are you sure you want to delete skill "${skillName}"?`)) return;
    try {
      await deleteSkill(skillName);
      if (selectedSkill?.name === skillName) {
        setSelectedSkill(null);
      }
      await loadData();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete skill');
    }
  };

  const filteredSkills = skills.filter(s => {
    if (filterSource === 'all') return true;
    return s.source === filterSource;
  });

  const userSkills = skills.filter(s => s.source === 'user');
  const projectSkills = skills.filter(s => s.source === 'project');

  const loadEvolutionData = useCallback(async () => {
    setEvoLoading(true);
    try {
      const statuses = await Promise.all(
        skills.map(async (s) => {
          try {
            const status = await fetchSkillEvolutionStatus(s.name);
            return [s.name, status] as const;
          } catch {
            return null;
          }
        })
      );
      const map = new Map<string, SkillEvolutionStatus>();
      for (const entry of statuses) {
        if (entry) {
          map.set(entry[0], entry[1]);
        }
      }
      setEvoSkillStatuses(map);
    } catch (err) {
      console.error('Failed to load evolution data:', err);
    } finally {
      setEvoLoading(false);
    }
  }, [skills]);

  useEffect(() => {
    if (activeTab === 'evolution' && skills.length > 0) {
      loadEvolutionData();
    }
  }, [activeTab, skills, loadEvolutionData]);

  const loadEvoSkillDetail = useCallback(async (skillName: string) => {
    setEvoSelectedSkill(skillName);
    try {
      const [pool, champion, prov] = await Promise.all([
        fetchReplayPool(skillName).catch(() => null),
        fetchChampion(skillName).catch(() => null),
        fetchSkillProvenance(skillName).catch(() => []),
      ]);
      setEvoReplayPool(pool);
      setEvoChampion(champion);
      setEvoProvenance(prov);
    } catch (err) {
      console.error('Failed to load evolution detail:', err);
    }
  }, []);

  const statusConfig: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
    healthy: { color: 'text-green-600 bg-green-50 border-green-200', icon: <CheckCircle className="w-3 h-3" />, label: 'Healthy' },
    watch: { color: 'text-yellow-600 bg-yellow-50 border-yellow-200', icon: <AlertTriangle className="w-3 h-3" />, label: 'Watch' },
    incubating: { color: 'text-indigo-600 bg-indigo-50 border-indigo-200', icon: <Circle className="w-3 h-3" />, label: 'Incubating' },
    unobserved: { color: 'text-gray-500 bg-gray-50 border-gray-200', icon: <EyeOff className="w-3 h-3" />, label: 'Unobserved' },
  };

  const getStatusBadge = (status: string) => {
    const cfg = statusConfig[status] || statusConfig.unobserved;
    return (
      <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] font-medium ${cfg.color}`}>
        {cfg.icon}
        {cfg.label}
      </span>
    );
  };

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-gray-500">Loading skills...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-red-500">{error}</div>
      </div>
    );
  }

  return (
    <PageLayout
      sidebarContent={
        activeTab === 'skills' ? (
          <div className="flex flex-col h-full">
            <div className="p-3 border-b border-gray-200">
              <div className="flex items-center justify-between mb-2">
                <div>
                  <h2 className="text-sm font-semibold text-gray-900">Skills</h2>
                  <p className="text-[10px] text-gray-500 mt-0.5">
                    {skills.length} skills ({userSkills.length} user, {projectSkills.length} project)
                  </p>
                </div>
                <button
                  onClick={loadData}
                  className="p-1.5 text-gray-500 hover:bg-gray-100 rounded transition-colors"
                  title="Refresh"
                >
                  <RefreshCw className="w-3.5 h-3.5" />
                </button>
              </div>
              <div className="flex gap-1">
                {(['all', 'user', 'project'] as const).map(src => (
                  <button
                    key={src}
                    onClick={() => setFilterSource(src)}
                    className={`px-2 py-0.5 text-[10px] rounded transition-colors ${
                      filterSource === src
                        ? 'bg-indigo-100 text-indigo-700'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    {src === 'all' ? `All (${skills.length})` : src === 'user' ? `User (${userSkills.length})` : `Project (${projectSkills.length})`}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex-1 overflow-y-auto">
              <div className="divide-y divide-gray-100">
                {filteredSkills.map(skill => (
                  <div
                    key={skill.name}
                    onClick={() => handleSkillClick(skill)}
                    className={`px-3 py-2 cursor-pointer transition-colors group ${
                      selectedSkill?.name === skill.name
                        ? 'bg-indigo-50 border-l-2 border-indigo-500'
                        : 'hover:bg-gray-50'
                    } ${disabledSkills.has(skill.name) ? 'opacity-50' : ''}`}
                  >
                    <div className="flex items-center gap-1.5 mb-0.5">
                      {skill.source === 'user' ? (
                        <User className="w-3 h-3 text-purple-500" />
                      ) : (
                        <Folder className="w-3 h-3 text-green-500" />
                      )}
                      <span className="font-medium text-xs text-gray-900 truncate">{skill.name}</span>
                      <div className="ml-auto flex items-center gap-0.5 opacity-0 group-hover:opacity-100">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleSkill(skill.name);
                          }}
                          className={`p-0.5 rounded transition-colors ${
                            disabledSkills.has(skill.name)
                              ? 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                              : 'bg-green-100 text-green-700 hover:bg-green-200'
                          }`}
                          title={disabledSkills.has(skill.name) ? 'Enable skill' : 'Disable skill'}
                        >
                          <Power className="w-2.5 h-2.5" />
                        </button>
                      </div>
                    </div>
                    <p className="text-[10px] text-gray-500 line-clamp-2">{skill.description}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="p-3">
            <p className="text-xs text-gray-500">Evolution tab content</p>
          </div>
        )
      }
    >
    <div className="h-full flex flex-col bg-white">
      <div className="p-4 border-b border-gray-200">
        <div className="flex gap-4">
          <button
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'skills'
                ? 'text-indigo-600 border-b-2 border-indigo-600'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab('skills')}
          >
            <Wrench className="w-4 h-4 inline mr-2" />
            Skills ({skills.length})
          </button>
          <button
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'evolution'
                ? 'text-indigo-600 border-b-2 border-indigo-600'
                : 'text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab('evolution')}
          >
            <Activity className="w-4 h-4 inline mr-2" />
            Evolution
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {activeTab === 'skills' && (
          <div className="flex h-full">
            {/* Right: Skill detail / editor */}
            <div className="flex-1 flex flex-col overflow-hidden">
              {selectedSkill ? (
                <>
                  <div className="p-4 border-b border-gray-200 flex items-center justify-between">
                    <div>
                      <h2 className="text-lg font-bold text-gray-900">{selectedSkill.name}</h2>
                      <p className="text-sm text-gray-500">{selectedSkill.description}</p>
                      <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
                        <span className="flex items-center gap-1">
                          {selectedSkill.source === 'user' ? <User className="w-3 h-3" /> : <Folder className="w-3 h-3" />}
                          {selectedSkill.source}
                        </span>
                        <span className="font-mono">{selectedSkill.skill_dir}</span>
                      </div>
                    </div>
                    <div className="flex gap-2">
                      {isEditing ? (
                        <>
                          <button
                            onClick={handleSave}
                            disabled={saving}
                            className="flex items-center px-3 py-1.5 bg-green-500 text-white text-sm rounded hover:bg-green-600 disabled:opacity-50"
                          >
                            <Save className="w-4 h-4 mr-1" />
                            {saving ? 'Saving...' : 'Save'}
                          </button>
                          <button
                            onClick={() => { setIsEditing(false); setEditContent(selectedSkill.raw_content || ''); }}
                            className="flex items-center px-3 py-1.5 bg-gray-500 text-white text-sm rounded hover:bg-gray-600"
                          >
                            <X className="w-4 h-4 mr-1" />
                            Cancel
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            onClick={() => setIsEditing(true)}
                            className="flex items-center px-3 py-1.5 bg-indigo-500 text-white text-sm rounded hover:bg-indigo-600"
                          >
                            <Edit3 className="w-4 h-4 mr-1" />
                            Edit
                          </button>
                          <button
                            onClick={(e) => handleDelete(selectedSkill.name, e)}
                            className="flex items-center px-3 py-1.5 bg-red-500 text-white text-sm rounded hover:bg-red-600"
                          >
                            <Trash2 className="w-4 h-4 mr-1" />
                            Delete
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="flex-1 overflow-y-auto p-4">
                    {isEditing ? (
                      <textarea
                        value={editContent}
                        onChange={(e) => setEditContent(e.target.value)}
                        className="w-full h-full font-mono text-sm bg-gray-50 border border-gray-300 rounded p-4 resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500"
                        spellCheck={false}
                      />
                    ) : (
                      <pre className="whitespace-pre-wrap text-sm text-gray-700 font-mono bg-gray-50 rounded p-4 overflow-x-auto">
                        {selectedSkill.raw_content || 'No content available'}
                      </pre>
                    )}
                  </div>
                </>
              ) : (
                <div className="flex-1 flex items-center justify-center text-gray-400">
                  Select a skill to view details
                </div>
              )}
            </div>
          </div>
        )}

        {activeTab === 'evolution' && (
          <div className="h-full flex flex-col">
            <div className="p-4 border-b border-gray-200 flex items-center justify-between">
              <div>
                <h2 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                  <Activity className="w-5 h-5 text-indigo-500" />
                  Skill Evolution
                </h2>
                <p className="text-xs text-gray-500 mt-0.5">
                  {evoSkillStatuses.size} skills evaluated
                  {evoLoading && ' (loading...)'}
                </p>
              </div>
              <button
                onClick={loadEvolutionData}
                disabled={evoLoading}
                className="flex items-center gap-1 px-3 py-1.5 bg-indigo-500 text-white rounded text-xs hover:bg-indigo-600 transition-colors disabled:opacity-50"
              >
                <RefreshCw className={`w-3 h-3 ${evoLoading ? 'animate-spin' : ''}`} />
                Refresh
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              <div className="bg-gray-50 rounded-lg border border-gray-200 overflow-hidden">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-gray-100 border-b border-gray-200">
                      <th className="text-left px-3 py-2 font-medium text-gray-600">Skill</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-600">Status</th>
                      <th className="text-right px-3 py-2 font-medium text-gray-600">Replay</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-600">Champion</th>
                    </tr>
                  </thead>
                  <tbody>
                    {skills.map(skill => {
                      const evoStatus = evoSkillStatuses.get(skill.name);
                      return (
                        <tr
                          key={skill.name}
                          className={`border-b border-gray-100 cursor-pointer hover:bg-indigo-50 transition-colors ${
                            evoSelectedSkill === skill.name ? 'bg-indigo-50' : ''
                          }`}
                          onClick={() => loadEvoSkillDetail(skill.name)}
                        >
                          <td className="px-3 py-2">
                            <span className="font-medium text-gray-900">{skill.name}</span>
                          </td>
                          <td className="px-3 py-2">
                            {evoStatus ? getStatusBadge(evoStatus.status) : (
                              <span className="text-gray-400">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-right font-mono text-gray-700">
                            {evoStatus?.replay_pool_size ?? '—'}
                          </td>
                          <td className="px-3 py-2">
                            {evoStatus?.champion?.version ? (
                              <span className="px-1.5 py-0.5 bg-yellow-100 text-yellow-700 rounded text-[10px] font-mono">
                                <Star className="w-2.5 h-2.5 inline mr-0.5" />
                                {evoStatus.champion.version}
                              </span>
                            ) : (
                              <span className="text-gray-400">—</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {evoSelectedSkill && evoSkillStatuses.get(evoSelectedSkill) && (
                <SkillEvolutionDetail
                  skillName={evoSelectedSkill}
                  status={evoSkillStatuses.get(evoSelectedSkill)!}
                  replayPool={evoReplayPool}
                  champion={evoChampion}
                  provenance={evoProvenance}
                  getStatusBadge={getStatusBadge}
                />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
    </PageLayout>
  );
}
