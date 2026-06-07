process TRIMMOMATIC {
    tag "$meta.id"
    label 'process_medium'

    conda (params.enable_conda ? "bioconda::trimmomatic=0.39" : null)
    container "${ workflow.containerEngine == 'singularity' && !task.ext.singularity_pull_docker_container ?
        'https://depot.galaxyproject.org/singularity/trimmomatic:0.39--hdfd78af_2' :
        'quay.io/biocontainers/trimmomatic:0.39--hdfd78af_2' }"

    input:
    tuple val(meta), path(reads)
    path(adapter_fasta)

    output:
    tuple val(meta), path("*.paired.trim*.fastq.gz"), emit: trimmed_reads
    tuple val(meta), path("*.unpaired.trim*.fastq.gz"), optional: true, emit: unpaired_reads
    path "*.log", emit: log
    path "*.summary", emit: summary

    script:
    def args = task.ext.args ?: 'ILLUMINACLIP:TruSeq3-PE.fa:2:30:10:2:keepBothReads LEADING:3 TRAILING:3 MINLEN:36'
    def prefix = task.ext.prefix ?: "${meta.id}"
    def trimmed = meta.single_end ? "${prefix}.trim.fastq.gz" : "${prefix}.paired.trim_1.fastq.gz ${prefix}.unpaired.trim_1.fastq.gz ${prefix}.paired.trim_2.fastq.gz ${prefix}.unpaired.trim_2.fastq.gz"
    def input_command = meta.single_end ? "${reads}" : "${reads[0]} ${reads[1]}"
    """
    trimmomatic \
        ${meta.single_end ? 'SE' : 'PE'} \
        -threads ${task.cpus} \
        -phred33 \
        ${input_command} \
        ${trimmed} \
        ${args} \
        2> ${prefix}.log

    cat ${prefix}.log > ${prefix}.summary
    """
}
