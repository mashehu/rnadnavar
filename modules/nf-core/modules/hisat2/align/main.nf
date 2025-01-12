def VERSION = '2.2.1' // Version information not provided by tool on CLI

process HISAT2_ALIGN {
    tag "$meta.id"
    label 'process_high'

    conda (params.enable_conda ? "bioconda::hisat2=2.2.1 bioconda::samtools=1.15.1" : null)
    container "${ workflow.containerEngine == 'singularity' && !task.ext.singularity_pull_docker_container ?
        'https://depot.galaxyproject.org/singularity/mulled-v2-a97e90b3b802d1da3d6958e0867610c718cb5eb1:38aed4501da19db366dc7c8d52d31d94e760cfaf-0' :
        'quay.io/biocontainers/mulled-v2-a97e90b3b802d1da3d6958e0867610c718cb5eb1:38aed4501da19db366dc7c8d52d31d94e760cfaf-0' }"

    input:
    tuple val(meta), path(reads)
    path  index
    path  splicesites

    output:
    tuple val(meta), path("*.bam")                   , emit: bam
    tuple val(meta), path("*.log")                   , emit: summary
    tuple val(meta), path("*fastq.gz"), optional:true, emit: fastq
    path  "versions.yml"                             , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.id}"
    def rg_tag = meta.rg_id ? "--rg-id ID:${meta.rg_id} --rg SM:${meta.rg_sm} --rg PU:${meta.rg_pu} --rg LB:${meta.rg_lb} --rg PL:${meta.rg_pl}" : ''
    def strandedness = ''
    if (meta.strandedness == 'forward') {
        strandedness = meta.single_end ? '--rna-strandness F' : '--rna-strandness FR'
    } else if (meta.strandedness == 'reverse') {
        strandedness = meta.single_end ? '--rna-strandness R' : '--rna-strandness RF'
    }
    if (params.single_end) {
        def unaligned = params.save_unaligned ? "--un-gz ${prefix}.unmapped.fastq.gz" : ''

        """
        INDEX=`find -L ./ -name "*.1.ht2" | sed 's/.1.ht2//'`
        hisat2 \\
            -x \$INDEX \\
            -U $reads \\
            $strandedness \\
            --known-splicesite-infile $splicesites \\
            --summary-file ${prefix}.hisat2.summary.log \\
            --threads $task.cpus \\
            $rg_tag \\
            $unaligned \\
            $args \\
            | samtools view -bS -F 4 -F 256 - > ${prefix}.bam

        cat <<-END_VERSIONS > versions.yml
        "${task.process}":
            hisat2: $VERSION
            samtools: \$(echo \$(samtools --version 2>&1) | sed 's/^.*samtools //; s/Using.*\$//')
        END_VERSIONS
        """
    } else {
        def unaligned = params.save_unaligned ? "--un-conc-gz ${prefix}.unmapped.fastq.gz" : ''
        """
        INDEX=`find -L ./ -name "*.1.ht2" | sed 's/.1.ht2//'`
        hisat2 \\
            -x \$INDEX \\
            -1 ${reads[0]} \\
            -2 ${reads[1]} \\
            $strandedness \\
            --known-splicesite-infile $splicesites \\
            --summary-file ${prefix}.hisat2.summary.log \\
            --threads $task.cpus \\
            $rg_tag \\
            $unaligned \\
            $args \\
            | samtools view -bS -F 4 -F 8 -F 256 - > ${prefix}.bam

        if [ -f ${prefix}.unmapped.fastq.1.gz ]; then
            mv ${prefix}.unmapped.fastq.1.gz ${prefix}.unmapped_1.fastq.gz
        fi
        if [ -f ${prefix}.unmapped.fastq.2.gz ]; then
            mv ${prefix}.unmapped.fastq.2.gz ${prefix}.unmapped_2.fastq.gz
        fi

        cat <<-END_VERSIONS > versions.yml
        "${task.process}":
            hisat2: $VERSION
            samtools: \$(echo \$(samtools --version 2>&1) | sed 's/^.*samtools //; s/Using.*\$//')
        END_VERSIONS
        """
    }
}
