import { useState, useEffect } from 'react';
import { fetchSkills, fetchSkillEvolutionReport, fetchSkillEvolutionProvenance, fetchSkillEvolutionUsage, deleteSkill, Skill } from '../api/client';
import { Wrench, RefreshCw, Activity, Edit3, Save, X, User, Folder, ChevronDown, ChevronRight, Trash2, Info } from 'lucide-react';

interface SkillDetail extends Skill {
  prompt_template?: string;
  raw_content?: string;
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<SkillDetail | null>(null);
  const [editContent, setEditContent] = useState<string>('');
  const [isEditing, setIsEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [evolutionReport, setEvolutionReport] = useState<any>(null);
  const [evolutionProvenance, setEvolutionProvenance] = useState<any[]>([]);
  const [evolutionUsage, setEvolutionUsage] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'skills' | 'evolution'>('skills');
  const [filterSource, setFilterSource] = useState<'all' | 'user' | 'project'>('all');
  const [expandedProvenance, setExpandedProvenance] = useState<number | null>(null);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [skillsData, reportData, provenanceData, usageData] = await Promise.all([
        fetchSkills(),
        fetchSkillEvolutionReport(),
        fetchSkillEvolutionProvenance(),
        fetchSkillEvolutionUsage(),
      ]);
      setSkills(skillsData);
      setEvolutionReport(reportData);
      setEvolutionProvenance(provenanceData);
      setEvolutionUsage(usageData);
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
    <div className="h-full flex flex-col bg-white">
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Skills</h1>
            <p className="text-sm text-gray-500 mt-1">
              {skills.length} skills ({userSkills.length} user, {projectSkills.length} project)
            </p>
          </div>
          <button
            onClick={loadData}
            className="flex items-center px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 transition-colors"
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </button>
        </div>

        <div className="flex gap-4 border-b border-gray-200">
          <button
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === 'skills'
                ? 'text-blue-600 border-b-2 border-blue-600'
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
                ? 'text-blue-600 border-b-2 border-blue-600'
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
            {/* Left: Skills list */}
            <div className="w-80 border-r border-gray-200 overflow-y-auto flex-shrink-0">
              <div className="p-3 border-b border-gray-100">
                <div className="flex gap-1">
                  {(['all', 'user', 'project'] as const).map(src => (
                    <button
                      key={src}
                      onClick={() => setFilterSource(src)}
                      className={`px-2 py-1 text-xs rounded transition-colors ${
                        filterSource === src
                          ? 'bg-blue-100 text-blue-700'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                      }`}
                    >
                      {src === 'all' ? `All (${skills.length})` : src === 'user' ? `User (${userSkills.length})` : `Project (${projectSkills.length})`}
                    </button>
                  ))}
                </div>
              </div>
              <div className="divide-y divide-gray-100">
                {filteredSkills.map(skill => (
                  <div
                    key={skill.name}
                    onClick={() => handleSkillClick(skill)}
                    className={`p-3 cursor-pointer transition-colors ${
                      selectedSkill?.name === skill.name
                        ? 'bg-blue-50 border-l-2 border-blue-500'
                        : 'hover:bg-gray-50'
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      {skill.source === 'user' ? (
                        <User className="w-3 h-3 text-purple-500" />
                      ) : (
                        <Folder className="w-3 h-3 text-green-500" />
                      )}
                      <span className="font-medium text-sm text-gray-900">{skill.name}</span>
                      <span className={`ml-auto px-1.5 py-0.5 text-xs rounded ${
                        skill.source === 'user' ? 'bg-purple-100 text-purple-700' : 'bg-green-100 text-green-700'
                      }`}>
                        {skill.source}
                      </span>
                    </div>
                    <p className="text-xs text-gray-500 line-clamp-2">{skill.description}</p>
                  </div>
                ))}
              </div>
            </div>

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
                            className="flex items-center px-3 py-1.5 bg-blue-500 text-white text-sm rounded hover:bg-blue-600"
                          >
                            <Edit3 className="w-4 h-4 mr-1" />
                            Edit
                          </button>
                          {selectedSkill.source === 'user' && (
                            <button
                              onClick={(e) => handleDelete(selectedSkill.name, e)}
                              className="flex items-center px-3 py-1.5 bg-red-500 text-white text-sm rounded hover:bg-red-600"
                            >
                              <Trash2 className="w-4 h-4 mr-1" />
                              Delete
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                  <div className="flex-1 overflow-y-auto p-4">
                    {isEditing ? (
                      <textarea
                        value={editContent}
                        onChange={(e) => setEditContent(e.target.value)}
                        className="w-full h-full font-mono text-sm bg-gray-50 border border-gray-300 rounded p-4 resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
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
          <div className="p-6 space-y-6">
            {/* Introduction */}
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
              <div className="flex items-start gap-3">
                <Info className="w-5 h-5 text-blue-600 flex-shrink-0 mt-0.5" />
                <div className="text-sm text-gray-700">
                  <p className="font-semibold text-blue-900 mb-2">What is Skill Evolution?</p>
                  <p className="mb-2">
                    Skills can automatically improve through usage. When you use a skill, the system analyzes the interaction 
                    and may suggest improvements to the skill's prompt template. This creates a feedback loop where skills 
                    become more effective over time.
                  </p>
                  <p className="mb-2">
                    <strong>How it works:</strong> After each skill invocation, the system evaluates the outcome and generates 
                    evolution suggestions. These suggestions are tracked in the provenance log, showing how skills have changed 
                    and why.
                  </p>
                  <p>
                    <strong>Below you'll find:</strong> Evolution reports showing improvement metrics, provenance logs tracking 
                    all changes, and usage statistics showing how often each skill is invoked.
                  </p>
                </div>
              </div>
            </div>

            {/* Evolution Report */}
            <div>
              <h2 className="text-lg font-semibold text-gray-900 mb-3 flex items-center gap-2">
                <Activity className="w-5 h-5 text-blue-500" />
                Evolution Report
              </h2>
              <p className="text-sm text-gray-600 mb-3">
                Shows improvement metrics for skills that have undergone evolution. Higher scores indicate better performance after evolution.
              </p>
              {evolutionReport?.status === 'no_report' ? (
                <div className="text-sm text-gray-500 bg-gray-50 rounded-lg p-4">No evolution report available</div>
              ) : evolutionReport?.status === 'ok' && evolutionReport.report ? (
                <div className="space-y-4">
                  {Object.entries(evolutionReport.report).map(([key, value]) => (
                    <div key={key} className="bg-gray-50 rounded-lg p-4 border border-gray-200">
                      <h3 className="text-sm font-semibold text-gray-700 mb-2 uppercase tracking-wider">{key}</h3>
                      {typeof value === 'object' && value !== null ? (
                        <div className="space-y-2">
                          {Object.entries(value as Record<string, any>).map(([k, v]) => (
                            <div key={k} className="flex items-start gap-2">
                              <span className="text-xs font-medium text-gray-500 min-w-[120px]">{k}:</span>
                              <span className="text-sm text-gray-900">
                                {typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span className="text-sm text-gray-900">{String(value)}</span>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-red-500">Error: {evolutionReport?.error || 'Unknown error'}</div>
              )}
            </div>

            {/* Provenance Log */}
            <div>
              <h2 className="text-lg font-semibold text-gray-900 mb-3 flex items-center gap-2">
                <Activity className="w-5 h-5 text-purple-500" />
                Provenance Log ({evolutionProvenance.length} entries)
              </h2>
              {evolutionProvenance.length === 0 ? (
                <div className="text-sm text-gray-500 bg-gray-50 rounded-lg p-4">No provenance data available</div>
              ) : (
                <div className="space-y-2">
                  {evolutionProvenance.map((entry, idx) => (
                    <div key={idx} className="bg-gray-50 rounded-lg border border-gray-200 overflow-hidden">
                      <div
                        className="p-3 cursor-pointer hover:bg-gray-100 flex items-center justify-between"
                        onClick={() => setExpandedProvenance(expandedProvenance === idx ? null : idx)}
                      >
                        <div className="flex items-center gap-3">
                          {expandedProvenance === idx ? (
                            <ChevronDown className="w-4 h-4 text-gray-400" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-gray-400" />
                          )}
                          <span className="text-sm font-medium text-gray-900">
                            {entry.action || entry.event || `Entry ${idx + 1}`}
                          </span>
                          {entry.skill_name && (
                            <span className="px-2 py-0.5 text-xs bg-blue-100 text-blue-700 rounded">
                              {entry.skill_name}
                            </span>
                          )}
                        </div>
                        {entry.timestamp && (
                          <span className="text-xs text-gray-400">{entry.timestamp}</span>
                        )}
                      </div>
                      {expandedProvenance === idx && (
                        <div className="p-3 border-t border-gray-200 bg-white">
                          <pre className="text-xs font-mono text-gray-700 whitespace-pre-wrap overflow-x-auto">
                            {JSON.stringify(entry, null, 2)}
                          </pre>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Usage Statistics */}
            <div>
              <h2 className="text-lg font-semibold text-gray-900 mb-3 flex items-center gap-2">
                <Activity className="w-5 h-5 text-green-500" />
                Usage Statistics
              </h2>
              {Object.keys(evolutionUsage || {}).length === 0 ? (
                <div className="text-sm text-gray-500 bg-gray-50 rounded-lg p-4">No usage data available</div>
              ) : (
                <div className="space-y-3">
                  {Object.entries(evolutionUsage).map(([skillName, stats]) => (
                    <div key={skillName} className="bg-gray-50 rounded-lg p-4 border border-gray-200">
                      <h3 className="text-sm font-semibold text-gray-900 mb-2">{skillName}</h3>
                      {typeof stats === 'object' && stats !== null ? (
                        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                          {Object.entries(stats as Record<string, any>).map(([k, v]) => (
                            <div key={k} className="bg-white rounded p-2 border border-gray-100">
                              <div className="text-xs text-gray-500">{k}</div>
                              <div className="text-sm font-semibold text-gray-900">{String(v)}</div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span className="text-sm text-gray-900">{String(stats)}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
